# Copyright (C) 2007, Red Hat, Inc.
# Copyright (C) 2007 Collabora Ltd. <http://www.collabora.co.uk/>
# Copyright 2008 One Laptop Per Child
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
from string import ascii_letters, digits
try:
    from hashlib import sha1
except ImportError:
    # Python < 2.5
    from sha import new as sha1

import dbus
from dbus.exceptions import DBusException
import gobject
import socket
import struct


_logger = logging.getLogger('s-p-s.psutils')

_ASCII_ALNUM = ascii_letters + digits


PRESENCE_INTERFACE = "org.laptop.Sugar.Presence"


class NotJoinedError(DBusException):
    def __init__(self, msg):
        DBusException.__init__(self, msg)
        self._dbus_error_name = PRESENCE_INTERFACE + '.NotJoined'


class NotFoundError(DBusException):
    def __init__(self, msg):
        DBusException.__init__(self, msg)
        self._dbus_error_name = PRESENCE_INTERFACE + '.NotFound'


class WrongConnectionError(DBusException):
    def __init__(self, msg):
        DBusException.__init__(self, msg)
        self._dbus_error_name = PRESENCE_INTERFACE + '.WrongConnection'


def throw_into_callback(async_err_cb, exc):
    # Made necessary by https://bugs.freedesktop.org/show_bug.cgi?id=12403
    # When that bug is fixed, replace:
    #   throw_into_callback(async_err_cb, SomeError('foo'))
    # with the more obvious:
    #   async_err_cb(SomeError('foo'))
    try:
        raise exc
    except:
        async_err_cb(exc)


def pubkey_to_keyid(key):
    """Return the key ID for the given public key. This is currently its SHA-1
    in hex.

    :Parameters:
        `key` : str
            The public key as a Base64 string
    :Returns:
        The key ID as a string of hex digits
    """
    return sha1(key).hexdigest()


def escape_identifier(identifier):
    """Escape the given string to be a valid D-Bus object path or service
    name component, using a reversible encoding to ensure uniqueness.

    The reversible encoding is as follows:

    * The empty string becomes '_'
    * Otherwise, each non-alphanumeric character is replaced by '_' plus
      two lower-case hex digits; the same replacement is carried out on
      the first character, if it's a digit
    """
    # '' -> '_'
    if not identifier:
        return '_'

    # A bit of a fast path for strings which are already OK.
    # We deliberately omit '_' because, for reversibility, that must also
    # be escaped.
    if (identifier.strip(_ASCII_ALNUM) == '' and
        identifier[0] in ascii_letters):
        return identifier

    # The first character may not be a digit
    if identifier[0] not in ascii_letters:
        ret = ['_%02x' % ord(identifier[0])]
    else:
        ret = [identifier[0]]

    # Subsequent characters may be digits or ASCII letters
    for c in identifier[1:]:
        if c in _ASCII_ALNUM:
            ret.append(c)
        else:
            ret.append('_%02x' % ord(c))

    return ''.join(ret)


_NM_SERVICE = 'org.freedesktop.NetworkManager'
_NM_IFACE = 'org.freedesktop.NetworkManager'
_NM_IFACE_DEVICE = 'org.freedesktop.NetworkManager.Device'
_NM_PATH = '/org/freedesktop/NetworkManager'
_NM_DEVICE_STATE_ACTIVATED = 8
_NM_STATE_CONNECTED = 3
_NM_STATE_DISCONNECTED = 4
_NM_ACTIVE_CONN_IFACE = 'org.freedesktop.NetworkManager.Connection.Active'
_DBUS_PROPERTIES = 'org.freedesktop.DBus.Properties'

_ip4am = None

class IP4AddressMonitor(gobject.GObject):
    """Monitor NetworkManager for IP4 address changes."""

    __gsignals__ = {
        'address-changed': (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                               ([gobject.TYPE_PYOBJECT, gobject.TYPE_STRING]))
    }

    __gproperties__ = {
        'address' : (str, None, None, None, gobject.PARAM_READABLE)
    }

    def get_instance():
        """Retrieve (or create) the IP4Address monitor singleton instance"""
        global _ip4am
        if not _ip4am:
            _ip4am = IP4AddressMonitor()
        return _ip4am
    get_instance = staticmethod(get_instance)

    def __init__(self):
        gobject.GObject.__init__(self)
        self._nm_present = False
        self._nm_has_been_present = False
        self._matches = []
        self._addr = None
        self._nm_iface = None
        self._sys_bus = None
        self._watch = None
        self._find_network_manager()

    def _find_network_manager(self):
        found = False
        try:
            self._sys_bus = dbus.SystemBus()
            self._watch = self._sys_bus.watch_name_owner(_NM_SERVICE,
                self._nm_owner_cb)
            found = self._sys_bus.name_has_owner(_NM_SERVICE)

        except DBusException:
            _logger.exception('Error connecting to NetworkManager')

        if not found:
            addr, iface = self._get_address_fallback()
            self._update_address(addr, iface)

    def do_get_property(self, pspec):
        if pspec.name == "address":
            return self._addr

    def _update_address(self, new_addr, iface):
        if new_addr == "0.0.0.0":
            new_addr = None
        if new_addr == self._addr:
            return

        self._addr = new_addr
        _logger.debug("IP4 address now '%s' (%s)" % (new_addr, iface))
        self.emit('address-changed', new_addr, iface)

    def _connect_to_nm(self):
        """Connect to NM device state signals to watch IPv4 address changes"""
        try:
            nm_obj = self._sys_bus.get_object(_NM_SERVICE, _NM_PATH)
            self._nm_iface = dbus.Interface(nm_obj, _NM_IFACE)
        except DBusException, err:
            _logger.debug("Error finding NetworkManager: %s" % err)
            self._nm_present = False
            addr, iface = self._get_address_fallback()
            self._update_address(addr, iface)
            return

        # Detect NM 0.6 which is now unsupported, so we can use the fallback
        try:
            dummy = self._nm_iface.GetDevices()
        except DBusException:
            _logger.debug(
                "Error NM 0.6 is now unsupported - we use the fallback.")
            addr, iface = self._get_address_fallback()
            self._update_address(addr, iface)
            return

        match = self._sys_bus.add_signal_receiver(
            self._nm_device_added_cb, signal_name="DeviceAdded",
            dbus_interface=_NM_IFACE, bus_name=_NM_SERVICE)
        self._matches.append(match)

        match = self._sys_bus.add_signal_receiver(
            self._nm_device_removed_cb,
            signal_name="DeviceRemoved",
            dbus_interface=_NM_IFACE, bus_name=_NM_SERVICE)
        self._matches.append(match)

        match = self._sys_bus.add_signal_receiver(
            self._nm_state_change_cb, signal_name="StateChanged",
            dbus_interface=_NM_IFACE, bus_name=_NM_SERVICE)
        self._matches.append(match)

        nm_props = dbus.Interface(nm_obj, _DBUS_PROPERTIES)
        state = nm_props.Get(_NM_IFACE, 'State')
        if state == _NM_STATE_CONNECTED:
            self._query_devices()

    def _query_device_properties(self, device_path):
        """Query a device's properties to get IP address and interface."""
        device = self._sys_bus.get_object(_NM_SERVICE, device_path)
        props = dbus.Interface(device, _DBUS_PROPERTIES)
        state = props.Get(_NM_IFACE_DEVICE, 'State')
        if state == _NM_DEVICE_STATE_ACTIVATED:
            ip = props.Get(_NM_IFACE_DEVICE, 'Ip4Address')
            ipaddr = socket.inet_ntoa(struct.pack('I', ip))
            iface = props.Get(_NM_IFACE_DEVICE, 'Interface')
            self._update_address(ipaddr, iface)

    def _get_devices_cb(self, device_paths):
        """Query each device's properties"""
        for device_path in device_paths:
            self._query_device_properties(device_path)

    def _get_devices_error_cb(self, err):
        _logger.debug("Error getting NetworkManager devices: %s" % err)

    def _query_devices(self):
        """Query NM for a list of network devices"""
        _nm_props = dbus.Interface(self._nm_iface, _DBUS_PROPERTIES)
        active_connection_paths = _nm_props.Get(_NM_IFACE, 'ActiveConnections')
        for conn_path in active_connection_paths:
            conn = self._sys_bus.get_object(_NM_IFACE, conn_path)
            conn_props = dbus.Interface(conn, _DBUS_PROPERTIES)
            conn_props.Get(_NM_ACTIVE_CONN_IFACE, 'Devices',
                           reply_handler=self._get_devices_cb,
                           error_handler=self._get_devices_error_cb)

    def _nm_device_added_cb(self, device_path):
        """Handle NetworkManager DeviceAdded signal."""
        self._query_device_properties(device_path)

    def _nm_device_removed_cb(self, device_path):
        """Handle NetworkManager DeviceRemoved signal."""
        self._update_address(None, None)

    def _nm_state_change_cb(self, new_state):
        """Handle NetworkManager StateChanged signal."""
        if new_state == _NM_STATE_DISCONNECTED:
            self._update_address(None, None)
        elif new_state == _NM_STATE_CONNECTED:
            self._query_devices()

    def _nm_owner_cb(self, unique_name):
        """Clear state when NM goes away"""
        if unique_name == '':
            # NM went away, or isn't there at all
            self._nm_present = False
            for match in self._matches:
                self._sys_bus.remove_signal_receiver(match)
                match.remove()
            self._matches = []
            if self._nm_has_been_present:
                self._update_address(None, None)
            else:
                addr, iface = self._get_address_fallback()
                self._update_address(addr, iface)
        elif not self._nm_present:
            # NM started up
            self._nm_present = True
            self._nm_has_been_present = True
            self._connect_to_nm()

    def _get_iface_address(self, iface):
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fd = s.fileno()
        SIOCGIFADDR = 0x8915
        addr = fcntl.ioctl(fd, SIOCGIFADDR,
                           struct.pack('256s', iface[:15]))[20:24]
        s.close()
        return socket.inet_ntoa(addr), iface

    def _get_address_fallback(self):
        import commands
        (s, o) = commands.getstatusoutput("/sbin/route -n")
        if s != 0:
            return None, None
        for line in o.split('\n'):
            fields = line.split(" ")
            if fields[0] == "0.0.0.0":
                iface = fields[len(fields) - 1]
                return self._get_iface_address(iface)
        return None, None
