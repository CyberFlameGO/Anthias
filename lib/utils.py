import certifi
import db
import json
import os
import pytz
import random
import re
import redis
import requests
import string
import sh
import time

from datetime import datetime, timedelta
from distutils.util import strtobool
from netifaces import ifaddresses, gateways, AF_INET, AF_LINK
from os import getenv, path, utime
from platform import machine
from settings import settings, ZmqPublisher
from subprocess import check_output, call
from threading import Thread
from urlparse import urlparse
import logging

from assets_helper import update

WOTT_PATH = '/opt/wott'

arch = machine()

# This will only work on the Raspberry Pi,
# so let's wrap it in a try/except so that
# Travis can run.
try:
    from sh import ffprobe
except ImportError:
    pass

def string_to_bool(string):
    return bool(strtobool(str(string)))


def touch(path):
    with open(path, 'a'):
        utime(path, None)


def is_ci():
    """
    Returns True when run on CI.
    """
    return string_to_bool(os.getenv('CI', False))


def validate_url(string):
    """Simple URL verification.
    >>> validate_url("hello")
    False
    >>> validate_url("ftp://example.com")
    False
    >>> validate_url("http://")
    False
    >>> validate_url("http://wireload.net/logo.png")
    True
    >>> validate_url("https://wireload.net/logo.png")
    True
    """

    checker = urlparse(string)
    return bool(checker.scheme in ('http', 'https', 'rtsp', 'rtmp') and checker.netloc)


def get_balena_supervisor_api_response(method, action):
    return getattr(requests, method)('{}/v1/{}?apikey={}'.format(
        os.getenv('BALENA_SUPERVISOR_ADDRESS'),
        action,
        os.getenv('BALENA_SUPERVISOR_API_KEY'),
    ), headers={'Content-Type': 'application/json'})


def get_balena_device_info():
    return get_balena_supervisor_api_response(method='get', action='device')


def shutdown_via_balena_supervisor():
    return get_balena_supervisor_api_response(method='post', action='shutdown')


def reboot_via_balena_supervisor():
    return get_balena_supervisor_api_response(method='post', action='reboot')


def get_node_ip():
    """
    Returns the node's IP address.
    We're using an API call to the supervisor for this on Balena
    and an environment variable set by `install.sh` for other environments.
    The reason for this is because we can't retrieve the host IP from within Docker.
    """

    if is_balena_app():
        response = get_balena_device_info()

        if response.ok:
            return response.json()['ip_address']
        return 'Unknown'
    elif os.getenv('MY_IP'):
        return os.getenv('MY_IP')

    return 'Unable to retrieve IP.'


def get_node_mac_address():
    """
    Returns the MAC address.
    """
    if is_balena_app():
        balena_supervisor_address = os.getenv('BALENA_SUPERVISOR_ADDRESS')
        balena_supervisor_api_key = os.getenv('BALENA_SUPERVISOR_API_KEY')
        headers = {'Content-Type': 'application/json'}

        r = requests.get('{}/v1/device?apikey={}'.format(
            balena_supervisor_address,
            balena_supervisor_api_key
        ), headers=headers)

        if r.ok:
            return r.json()['mac_address']
        return 'Unknown'

    return 'Unable to retrieve MAC address.'


def get_active_connections(bus, fields=None):
    """

    :param bus: pydbus.bus.Bus
    :param fields: list
    :return: list
    """
    if not fields:
        fields = ['Id', 'Uuid', 'Type', 'Devices']

    connections = list()

    try:
        nm_proxy = bus.get("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
    except Exception:
        return None

    nm_properties = nm_proxy["org.freedesktop.DBus.Properties"]
    active_connections = nm_properties.Get("org.freedesktop.NetworkManager", "ActiveConnections")
    for active_connection in active_connections:
        active_connection_proxy = bus.get("org.freedesktop.NetworkManager", active_connection)
        active_connection_properties = active_connection_proxy["org.freedesktop.DBus.Properties"]

        connection = dict()
        for field in fields:
            field_value = active_connection_properties.Get("org.freedesktop.NetworkManager.Connection.Active", field)

            if field == 'Devices':
                devices = list()
                for device_path in field_value:
                    device_proxy = bus.get("org.freedesktop.NetworkManager", device_path)
                    device_properties = device_proxy["org.freedesktop.DBus.Properties"]
                    devices.append(device_properties.Get("org.freedesktop.NetworkManager.Device", "Interface"))
                field_value = devices

            connection.update({field: field_value})
        connections.append(connection)

    return connections


def remove_connection(bus, uuid):
    """

    :param bus: pydbus.bus.Bus
    :param uuid: string
    :return: boolean
    """
    try:
        nm_proxy = bus.get("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings")
    except Exception:
        return False

    nm_settings = nm_proxy["org.freedesktop.NetworkManager.Settings"]

    connection_path = nm_settings.GetConnectionByUuid(uuid)
    connection_proxy = bus.get("org.freedesktop.NetworkManager", connection_path)
    connection = connection_proxy["org.freedesktop.NetworkManager.Settings.Connection"]
    connection.Delete()

    return True


def get_video_duration(file):
    """
    Returns the duration of a video file in timedelta.
    """
    time = None

    try:
        run_player = ffprobe('-i', file, _err_to_out=True)
    except sh.ErrorReturnCode_1:
        raise Exception('Bad video format')

    for line in run_player.split('\n'):
        if 'Duration' in line:
            match = re.search(r'[0-9]+:[0-9]+:[0-9]+\.[0-9]+', line)
            if match:
                time_input = match.group()
                time_split = time_input.split(':')
                hours = int(time_split[0])
                minutes = int(time_split[1])
                seconds = float(time_split[2])
                time = timedelta(hours=hours, minutes=minutes, seconds=seconds)
            break

    return time


def handler(obj):
    # Set timezone as UTC if it's datetime and format as ISO
    if isinstance(obj, datetime):
        with_tz = obj.replace(tzinfo=pytz.utc)
        return with_tz.isoformat()
    else:
        raise TypeError('Object of type %s with value of %s is not JSON serializable' % (type(obj), repr(obj)))


def json_dump(obj):
    return json.dumps(obj, default=handler)


def url_fails(url):
    """
    If it is streaming
    """
    if urlparse(url).scheme in ('rtsp', 'rtmp'):
        run_mplayer = mplayer('-identify', '-frames', '0', '-nosound', url)
        for line in run_mplayer.split('\n'):
            if 'Clip info:' in line:
                return False
        return True

    """
    Try HEAD and GET for URL availability check.
    """

    # Use Certifi module and set to True as default so users stop seeing InsecureRequestWarning in logs
    if settings['verify_ssl']:
        verify = certifi.where()
    else:
        verify = True

    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/538.15 (KHTML, like Gecko) Version/8.0 Safari/538.15'
    }
    try:
        if not validate_url(url):
            return False

        if requests.head(
            url,
            allow_redirects=True,
            headers=headers,
            timeout=10,
            verify=verify
        ).ok:
            return False

        if requests.get(
            url,
            allow_redirects=True,
            headers=headers,
            timeout=10,
            verify=verify
        ).ok:
            return False

    except (requests.ConnectionError, requests.exceptions.Timeout):
        pass

    return True


def download_video_from_youtube(uri, asset_id):
    home = getenv('HOME')
    name = check_output(['youtube-dl', '-e', uri])
    info = json.loads(check_output(['youtube-dl', '-j', uri]))
    duration = info['duration']

    location = path.join(home, 'screenly_assets', asset_id)
    thread = YoutubeDownloadThread(location, uri, asset_id)
    thread.daemon = True
    thread.start()

    return location, unicode(name.decode('utf-8')), duration


class YoutubeDownloadThread(Thread):
    def __init__(self, location, uri, asset_id):
        Thread.__init__(self)
        self.location = location
        self.uri = uri
        self.asset_id = asset_id

    def run(self):
        publisher = ZmqPublisher.get_instance()
        call(['youtube-dl', '-f', 'mp4', '-o', self.location, self.uri])
        with db.conn(settings['database']) as conn:
            update(conn, self.asset_id, {'asset_id': self.asset_id, 'is_processing': 0})

        publisher.send_to_ws_server(self.asset_id)


def template_handle_unicode(value):
    if isinstance(value, str):
        return value.decode('utf-8')
    return unicode(value)


def is_demo_node():
    """
    Check if the environment variable IS_DEMO_NODE is set to 1
    :return: bool
    """
    return string_to_bool(os.getenv('IS_DEMO_NODE', False))


def generate_perfect_paper_password(pw_length=10, has_symbols=True):
    """
    Generates a password using 64 characters from
    "Perfect Paper Password" system by Steve Gibson

    :param pw_length: int
    :param has_symbols: bool
    :return: string
    """
    ppp_letters = '!#%+23456789:=?@ABCDEFGHJKLMNPRSTUVWXYZabcdefghjkmnopqrstuvwxyz'
    if not has_symbols:
        ppp_letters = ''.join(set(ppp_letters) - set(string.punctuation))
    return "".join(random.SystemRandom().choice(ppp_letters) for _ in range(pw_length))


def connect_to_redis():
    return redis.Redis(host='redis', port=6379, db=0)

def is_docker():
    return os.path.isfile('/.dockerenv')


def is_balena_app():
    """
    Checks the application is running on Balena Cloud
    :return: bool
    """
    return bool(getenv('RESIN', False)) or bool(getenv('BALENA', False))


def is_wott_integrated():
    """
    Chacks if wott-agent installed or not
    :return:
    """
    return os.path.isdir(WOTT_PATH)


def get_wott_device_id():
    """
    :return: WoTT Device id of this device
    """
    metadata_path = os.path.join(WOTT_PATH, 'metadata.json')
    if os.path.isfile(metadata_path):
        with open(metadata_path) as metadata_file:
            metadata = json.load(metadata_file)
        if 'device_id' in metadata:
            return metadata['device_id']
    logging.warning("Could not read WoTT Device ID")
    return 'Could not read WoTT Device ID'
