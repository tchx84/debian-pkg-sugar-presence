"""An "actor" on the network, whether remote or local"""
# Copyright (C) 2007, Red Hat, Inc.
# Copyright (C) 2007, Collabora Ltd.
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

import os
import logging
try:
    # Python >= 2.5
    from hashlib import md5 as new_md5
except ImportError:
    from md5 import new as new_md5

import gobject
import gtk
import dbus
import dbus.service
from dbus.gobject_service import ExportedGObject
from telepathy.constants import CONNECTION_STATUS_CONNECTED
from telepathy.interfaces import (CONN_INTERFACE_ALIASING,
                                  CONN_INTERFACE_AVATARS)

from sugar import env, profile

import psutils
from buddyiconcache import buddy_icon_cache


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'

_BUDDY_PATH = "/org/laptop/Sugar/Presence/Buddies/"
_BUDDY_INTERFACE = "org.laptop.Sugar.Presence.Buddy"

_PROP_NICK = "nick"
_PROP_KEY = "key"
_PROP_ICON = "icon"
_PROP_CURACT = "current-activity"
_PROP_COLOR = "color"
_PROP_OWNER = "owner"
_PROP_VALID = "valid"
_PROP_OBJID = 'objid'

# Will go away soon
_PROP_IP4_ADDRESS = "ip4-address"

_logger = logging.getLogger('s-p-s.buddy')


def _noop(*args, **kwargs):
    pass

def _buddy_icon_save_cb(buf, data):
    data[0] += buf
    return True

def _get_buddy_icon_at_size(icon, maxw, maxh, maxsize):
    loader = gtk.gdk.PixbufLoader()
    loader.write(icon)
    loader.close()
    unscaled_pixbuf = loader.get_pixbuf()
    del loader

    pixbuf = unscaled_pixbuf.scale_simple(maxw, maxh, gtk.gdk.INTERP_BILINEAR)
    del unscaled_pixbuf

    data = [""]
    quality = 90
    img_size = maxsize + 1
    while img_size > maxsize:
        data = [""]
        pixbuf.save_to_callback(_buddy_icon_save_cb, "jpeg",
                                {"quality":"%d" % quality}, data)
        quality -= 10
        img_size = len(data[0])
    del pixbuf

    if img_size > maxsize:
        data = [""]
        raise RuntimeError("could not size image less than %d bytes" % maxsize)

    return str(data[0])


class Buddy(ExportedGObject):
    """Person on the network (tracks properties and shared activites)

    The Buddy is a collection of metadata describing a particular
    actor/person on the network.  The Buddy object tracks a set of
    activities which the actor has shared with the presence service.

    Buddies have a "valid" property which is used to flag Buddies
    which are no longer reachable.  That is, a Buddy may represent
    a no-longer reachable target on the network.

    The Buddy emits GObject events that the PresenceService uses
    to track changes in its status.

    Attributes:

        _activities -- dictionary mapping activity ID to
            activity.Activity objects
        _handles -- dictionary mapping Telepathy client plugin to
            tuples (contact handle, corresponding unique ID);
            channel-specific handles do not appear here
    """

    __gsignals__ = {
        'validity-changed':
            # The buddy's validity changed.
            # Validity starts off False, and becomes True when the buddy
            # has a color, a nick and a key.
            # * the new validity: bool
            (gobject.SIGNAL_RUN_FIRST, None, [bool]),
        'property-changed':
            # One of the buddy's properties has changed.
            # * those properties that have changed:
            #   dict { str => object }
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'icon-changed':
            # The buddy's icon changed.
            # * the bytes of the icon: str
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'disappeared':
            # The buddy is offline (has no Telepathy handles and is not the
            # Owner)
            (gobject.SIGNAL_RUN_FIRST, None, []),
    }

    __gproperties__ = {
        _PROP_KEY          : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_ICON         : (object, None, None, gobject.PARAM_READWRITE),
        _PROP_NICK         : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_COLOR        : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_CURACT       : (str, None, None, None, gobject.PARAM_READWRITE),
        _PROP_VALID        : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_OWNER        : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_OBJID        : (str, None, None, None, gobject.PARAM_READABLE),
        _PROP_IP4_ADDRESS  : (str, None, None, None, gobject.PARAM_READWRITE)
    }

    def __init__(self, bus, object_id, **kwargs):
        """Initialize the Buddy object

        bus -- connection to the D-Bus session bus
        object_id -- the buddy's unique identifier, either based on their
            key-ID or JID
        kwargs -- used to initialize the object's properties

        constructs a DBUS "object path" from the _BUDDY_PATH
        and object_id
        """

        self._object_id = object_id
        self._object_path = dbus.ObjectPath(_BUDDY_PATH + object_id)

        #: activity ID -> activity
        self._activities = {}
        self._activity_sigids = {}
        #: Telepathy plugin -> (handle, identifier e.g. JID)
        self._handles = {}

        self._valid = False
        self._owner = False
        self._key = None
        self._icon = ''
        self._current_activity = None
        self._nick = None
        self._color = None
        self._ip4_address = None

        _ALLOWED_INIT_PROPS = [_PROP_NICK, _PROP_KEY, _PROP_ICON,
                               _PROP_CURACT, _PROP_COLOR, _PROP_IP4_ADDRESS]
        for (key, value) in kwargs.items():
            if key not in _ALLOWED_INIT_PROPS:
                _logger.debug("Invalid init property '%s'; ignoring..." % key)
                del kwargs[key]

        # Set icon after superclass init, because it sends DBus and GObject
        # signals when set
        icon_data = None
        if kwargs.has_key(_PROP_ICON):
            icon_data = kwargs[_PROP_ICON]
            del kwargs[_PROP_ICON]

        ExportedGObject.__init__(self, bus, self._object_path,
                                 gobject_properties=kwargs)

        if icon_data:
            self.props.icon = icon_data

    def do_get_property(self, pspec):
        """Retrieve current value for the given property specifier

        pspec -- property specifier with a "name" attribute
        """
        if pspec.name == _PROP_OBJID:
            return self._object_id
        elif pspec.name == _PROP_KEY:
            return self._key
        elif pspec.name == _PROP_ICON:
            return self._icon
        elif pspec.name == _PROP_NICK:
            return self._nick
        elif pspec.name == _PROP_COLOR:
            return self._color
        elif pspec.name == _PROP_CURACT:
            if not self._current_activity:
                return None
            if not self._activities.has_key(self._current_activity):
                return None
            return self._current_activity
        elif pspec.name == _PROP_VALID:
            return self._valid
        elif pspec.name == _PROP_OWNER:
            return self._owner
        elif pspec.name == _PROP_IP4_ADDRESS:
            return self._ip4_address

    def do_set_property(self, pspec, value):
        """Set given property

        pspec -- property specifier with a "name" attribute
        value -- value to set

        emits 'icon-changed' signal on icon setting
        calls _update_validity on all calls
        """
        if pspec.name == _PROP_ICON:
            if str(value) != self._icon:
                self._icon = str(value)
                self.IconChanged(self._icon)
        elif pspec.name == _PROP_NICK:
            self._nick = value
        elif pspec.name == _PROP_COLOR:
            self._color = value
        elif pspec.name == _PROP_CURACT:
            self._current_activity = value
        elif pspec.name == _PROP_KEY:
            if self._key:
                raise RuntimeError("Key already set.")
            self._key = value
        elif pspec.name == _PROP_IP4_ADDRESS:
            self._ip4_address = value

        self._update_validity()

    # dbus signals
    @dbus.service.signal(_BUDDY_INTERFACE,
                        signature="ay")
    def IconChanged(self, icon_data):
        """Generates DBUS signal with icon_data"""

    @dbus.service.signal(_BUDDY_INTERFACE,
                        signature="o")
    def JoinedActivity(self, activity_path):
        """Generates DBUS signal when buddy joins activity

        activity_path -- DBUS path to the activity object
        """

    @dbus.service.signal(_BUDDY_INTERFACE,
                        signature="o")
    def LeftActivity(self, activity_path):
        """Generates DBUS signal when buddy leaves activity

        activity_path -- DBUS path to the activity object
        """

    @dbus.service.signal(_BUDDY_INTERFACE,
                        signature="a{sv}")
    def PropertyChanged(self, updated):
        """Generates DBUS signal when buddy's property changes

        updated -- updated property-set (dictionary) with the
            Buddy's property (changed) values. Note: not the
            full set of properties, just the changes.
        """

    def add_telepathy_handle(self, tp_client, handle, uid):
        """Add a Telepathy handle."""
        conn = tp_client.get_connection()
        self._handles[tp_client] = (handle, uid)
        self.TelepathyHandleAdded(conn.service_name, conn.object_path, handle)

    @dbus.service.signal(_BUDDY_INTERFACE, signature='sou')
    def TelepathyHandleAdded(self, tp_conn_name, tp_conn_path, handle):
        """Another Telepathy handle has become associated with the buddy.

        This must only be emitted for non-channel-specific handles.

        tp_conn_name -- The bus name at which the Telepathy connection may be
            found
        tp_conn_path -- The object path at which the Telepathy connection may
            be found
        handle -- The handle of type CONTACT, which is not channel-specific,
            newly associated with the buddy
        """

    def remove_telepathy_handle(self, tp_client):
        """Remove a Telepathy handle."""
        conn = tp_client.get_connection()
        try:
            handle, identifier = self._handles.pop(tp_client)
        except KeyError:
            return

        self.TelepathyHandleRemoved(conn.service_name, conn.object_path,
                                    handle)
        # the Owner can't disappear - that would be silly
        if not self._handles and not self._owner:
            self.emit('disappeared')

    @dbus.service.signal(_BUDDY_INTERFACE, signature='sou')
    def TelepathyHandleRemoved(self, tp_conn_name, tp_conn_path, handle):
        """A Telepathy handle has ceased to be associated with the buddy,
        probably because that contact went offline.

        The parameters are the same as for TelepathyHandleAdded.
        """

    # dbus methods
    @dbus.service.method(_BUDDY_INTERFACE,
                        in_signature="", out_signature="ay")
    def GetIcon(self):
        """Retrieve Buddy's icon data

        returns dbus.ByteArray
        """
        if not self.props.icon:
            return dbus.ByteArray('')
        return dbus.ByteArray(self.props.icon)

    @dbus.service.method(_BUDDY_INTERFACE,
                        in_signature="", out_signature="ao")
    def GetJoinedActivities(self):
        """Retrieve set of Buddy's joined activities (paths)

        returns list of dbus service paths for the Buddy's joined
            activities
        """
        acts = []
        for act in self.get_joined_activities():
            if act.props.valid:
                acts.append(act.object_path())
        return acts

    @dbus.service.method(_BUDDY_INTERFACE,
                        in_signature="", out_signature="a{sv}")
    def GetProperties(self):
        """Retrieve set of Buddy's properties

        returns dictionary of
            nick : str(nickname)
            owner : bool( whether this Buddy is an owner??? )
                XXX what is the owner flag for?
            key : str(public-key)
            color: Buddy's icon colour
                XXX what type?
            current-activity: Buddy's current activity_id, or
                "" if no current activity
        """
        props = {}
        props[_PROP_NICK] = self.props.nick
        props[_PROP_OWNER] = self.props.owner
        props[_PROP_KEY] = self.props.key
        props[_PROP_COLOR] = self.props.color

        if self.props.ip4_address:
            props[_PROP_IP4_ADDRESS] = self.props.ip4_address
        else:
            props[_PROP_IP4_ADDRESS] = ""

        if self.props.current_activity:
            props[_PROP_CURACT] = self.props.current_activity
        else:
            props[_PROP_CURACT] = ""
        return props

    @dbus.service.method(_BUDDY_INTERFACE,
                         in_signature='', out_signature='a(sou)')
    def GetTelepathyHandles(self):
        """Return a list of non-channel-specific Telepathy contact handles
        associated with this Buddy.

        :Returns:
            An array of triples (connection well-known bus name, connection
            object path, handle).
        """
        ret = []
        for plugin in self._handles:
            conn = plugin.get_connection()
            ret.append((str(conn.service_name), conn.object_path,
                        self._handles[plugin][0]))

    # methods
    def object_path(self):
        """Retrieve our dbus.ObjectPath object"""
        return dbus.ObjectPath(self._object_path)

    def _activity_validity_changed_cb(self, activity, valid):
        """Join or leave the activity when its validity changes"""
        if valid:
            self.JoinedActivity(activity.object_path())
        else:
            self.LeftActivity(activity.object_path())

    def add_activity(self, activity):
        """Add an activity to the Buddy's set of activities

        activity -- activity.Activity instance

        calls JoinedActivity
        """
        actid = activity.props.id
        if self._activities.has_key(actid):
            return
        self._activities[actid] = activity
        # join/leave activity when it's validity changes
        sigid = activity.connect("validity-changed",
                                 self._activity_validity_changed_cb)
        self._activity_sigids[actid] = sigid
        if activity.props.valid:
            self.JoinedActivity(activity.object_path())

    def remove_activity(self, activity):
        """Remove the activity from the Buddy's set of activities

        activity -- activity.Activity instance

        calls LeftActivity
        """
        actid = activity.props.id
        if not self._activities.has_key(actid):
            return
        activity.disconnect(self._activity_sigids[actid])
        del self._activity_sigids[actid]
        del self._activities[actid]
        if activity.props.valid:
            self.LeftActivity(activity.object_path())

    def get_joined_activities(self):
        """Retrieves list of still-valid activity objects"""
        acts = []
        for act in self._activities.values():
            acts.append(act)
        return acts

    def set_properties(self, properties):
        """Set the given set of properties on the object

        properties -- set of property values to set

        if no change, no events generated
        if change, generates property-changed and
            calls _update_validity
        """
        changed = False
        changed_props = {}
        if _PROP_NICK in properties:
            nick = properties[_PROP_NICK]
            if nick != self._nick:
                self._nick = nick
                changed_props[_PROP_NICK] = nick
                changed = True
        if _PROP_COLOR in properties:
            color = properties[_PROP_COLOR]
            if color != self._color:
                self._color = color
                changed_props[_PROP_COLOR] = color
                changed = True
        if _PROP_CURACT in properties:
            curact = properties[_PROP_CURACT]
            if curact != self._current_activity:
                self._current_activity = curact
                changed_props[_PROP_CURACT] = curact
                changed = True
        if _PROP_IP4_ADDRESS in properties:
            ip4addr = properties[_PROP_IP4_ADDRESS]
            if ip4addr != self._ip4_address:
                self._ip4_address = ip4addr
                changed_props[_PROP_IP4_ADDRESS] = ip4addr
                changed = True
        if _PROP_KEY in properties:
            # don't allow key to be set more than once
            if self._key is None:
                key = properties[_PROP_KEY]
                if key is not None:
                    self._key = key
                    changed_props[_PROP_KEY] = key
                    changed = True

        if not changed or not changed_props:
            return

        # Try emitting PropertyChanged before updating validity
        # to avoid leaking a PropertyChanged signal before the buddy is
        # actually valid the first time after creation
        if self._valid:
            dbus_changed = {}
            for key, value in changed_props.items():
                if value:
                    dbus_changed[key] = value
                else:
                    dbus_changed[key] = ""
            self.PropertyChanged(dbus_changed)

            self._property_changed(changed_props)

        self._update_validity()

    def _property_changed(self, changed_props):
        pass

    def _update_validity(self):
        """Check whether we are now valid

        validity is True if color, nick and key are non-null

        emits validity-changed if we have changed validity
        """
        try:
            old_valid = self._valid
            if self._color and self._nick and self._key:
                self._valid = True
            else:
                self._valid = False

            if old_valid != self._valid:
                self.emit("validity-changed", self._valid)
        except AttributeError:
            self._valid = False

    def update_avatar(self, tp, new_avatar_token):
        """Handle update of the avatar"""
        conn = tp.get_connection()
        handle, identifier = self._handles[tp]

        icon = buddy_icon_cache.get_icon(conn.object_path, identifier,
                                         new_avatar_token)
        if not icon:
            # cache miss
            def got_avatar(avatar, mime_type):
                icon = str(icon)
                buddy_icon_cache.store_icon(conn.object_path, identifier,
                                            new_avatar_token, icon)
                if self._icon != icon:
                    self._icon = icon
                    self.IconChanged(self._icon)

            conn[CONN_INTERFACE_AVATARS].RequestAvatar(handle,
                    reply_handler=got_avatar,
                    error_handler=lambda e:
                        _logger.warning('Error getting avatar for %r: %s',
                                        self, e),
                    byte_arrays=True)
        else:
            if self._icon != icon:
                self._icon = icon
                self.IconChanged(self._icon)


class GenericOwner(Buddy):
    """Common functionality for Local User-like objects

    The TestOwner wants to produce something *like* a
    ShellOwner, but with randomised changes and the like.
    This class provides the common features for a real
    local owner and a testing one.
    """
    __gtype_name__ = "GenericOwner"

    def __init__(self, ps, bus, object_id, **kwargs):
        """Initialize the GenericOwner instance

        ps -- presenceservice.PresenceService object
        bus -- a connection to the D-Bus session bus
        object_id -- the activity's unique identifier
        kwargs -- used to initialize the object's properties

        calls Buddy.__init__
        """
        self._ps = ps
        self._server = kwargs.pop("server", "olpc.collabora.co.uk")
        self._key_hash = kwargs.pop("key_hash", None)
        self._registered = kwargs.pop("registered", False)

        #: Telepathy plugin -> dict { activity ID -> room handle }
        self._activities_by_connection = {}

        self._ip4_addr_monitor = psutils.IP4AddressMonitor.get_instance()
        self._ip4_addr_monitor.connect("address-changed",
                                       self._ip4_address_changed_cb)
        if self._ip4_addr_monitor.props.address:
            kwargs["ip4-address"] = self._ip4_addr_monitor.props.address

        Buddy.__init__(self, bus, object_id, **kwargs)
        self._owner = True

        self._bus = bus

    def add_owner_activity(self, tp, activity_id, activity_room):
        # FIXME: this probably duplicates something else (_activities?)
        # but for now I'll keep the same duplication as before.
        # Equivalent code used to be in ServerPlugin.
        id_to_act = self._activities_by_connection.setdefault(tp, {})
        id_to_act[activity_id] = activity_room

        self._set_self_activities(tp)

    def _set_self_activities(self, tp):
        """Forward set of joined activities to network

        uses SetActivities on BuddyInfo channel
        """
        conn = tp.get_connection()
        conn[CONN_INTERFACE_BUDDY_INFO].SetActivities(
                self._activities_by_connection[tp].iteritems(),
                reply_handler=_noop,
                error_handler=lambda e:
                    _logger.warning("setting activities failed: %s", e))

    def _set_self_current_activity(self, tp):
        """Forward our current activity (or "") to network
        """
        cur_activity = self._current_activity
        if not cur_activity:
            cur_activity = ""
            cur_activity_handle = 0
        else:
            id_to_act = self._activities_by_connection.setdefault(tp, {})
            cur_activity_handle = id_to_act.get(cur_activity)
            if cur_activity_handle is None:
                # don't advertise a current activity that's not shared on
                # this connection
                # FIXME: this gives us a different current activity on each
                # connection - need to make sure clients are OK with this
                # (at the moment, PS isn't!)
                cur_activity = ""

        _logger.debug("Setting current activity to '%s' (handle %s)",
                      cur_activity, cur_activity_handle)
        conn = tp.get_connection()
        conn[CONN_INTERFACE_BUDDY_INFO].SetCurrentActivity(cur_activity,
                cur_activity_handle,
                reply_handler=_noop,
                error_handler=lambda e:
                    _logger.warning("setting current activity failed: %s", e))

    def _set_self_alias(self, tp):
        self_handle = self._handles[tp][0]
        conn = tp.get_connection()
        conn[CONN_INTERFACE_ALIASING].SetAliases({self_handle: self._nick},
                reply_handler=_noop,
                error_handler=lambda e:
                    _logger.warning('Error setting alias: %s', e))
        # Hack so we can use this as a timeout handler
        return False

    def _set_self_olpc_properties(self, tp):
        conn = tp.get_connection()
        # FIXME: omit color/key/ip4-address if None?
        conn[CONN_INTERFACE_BUDDY_INFO].SetProperties(
                {'color': self._color or '', 'key': self._key or '',
                 'ip4-address': self._ip4_address or '' },
                reply_handler=_noop,
                error_handler=lambda e:
                    _logger.warning('Error setting alias: %s', e))
        # Hack so we can use this as a timeout handler
        return False

    def add_telepathy_handle(self, tp_client, handle, uid):
        Buddy.add_telepathy_handle(self, tp_client, handle, uid)

        self._set_self_olpc_properties(tp_client)
        self._set_self_alias(tp_client)
        # Hack; send twice to make sure the server gets it
        gobject.timeout_add(1000, lambda: self._set_self_alias(tp_client))

        self._set_self_activities(tp_client)
        self._set_self_current_activity(tp_client)

        self._set_self_avatar(tp_client)

    def IconChanged(self, icon_data):
        # As well as emitting the D-Bus signal, prod the Telepathy
        # connection manager
        Buddy.IconChanged(self, icon_data)
        for tp in self._handles.iterkeys():
            self._set_self_avatar(tp)

    def _set_self_avatar(self, tp):
        conn = tp.get_connection()
        icon_data = self._icon

        m = new_md5()
        m.update(icon_data)
        digest = m.hexdigest()

        self_handle = self._handles[tp][0]
        token = conn[CONN_INTERFACE_AVATARS].GetAvatarTokens(
                [self_handle])[0]

        if buddy_icon_cache.check_avatar(conn.object_path, digest,
                                         token):
            # avatar is up to date
            return

        def set_self_avatar_cb(token):
            buddy_icon_cache.set_avatar(conn.object_path, digest, token)

        types, minw, minh, maxw, maxh, maxsize = \
                conn[CONN_INTERFACE_AVATARS].GetAvatarRequirements()
        if not "image/jpeg" in types:
            _logger.debug("server does not accept JPEG format avatars.")
            return

        img_data = _get_buddy_icon_at_size(icon_data, min(maxw, 96),
                                           min(maxh, 96), maxsize)
        conn[CONN_INTERFACE_AVATARS].SetAvatar(img_data, "image/jpeg",
                reply_handler=set_self_avatar_cb,
                error_handler=lambda e:
                    _logger.warning('Error setting avatar: %s', e))

    def _property_changed(self, changed_props):
        for tp in self._handles.iterkeys():

            if changed_props.has_key("current-activity"):
                self._set_self_current_activity(tp)

            if changed_props.has_key("nick"):
                self._set_self_alias(tp)
                # Hack; send twice to make sure the server gets it
                gobject.timeout_add(1000, lambda: self._set_self_alias(tp))

            if (changed_props.has_key("color") or
                changed_props.has_key("ip4-address")):
                if tp.status == CONNECTION_STATUS_CONNECTED:
                    self._set_self_olpc_properties(tp)

    def _ip4_address_changed_cb(self, monitor, address):
        """Handle IPv4 address change, set property to generate event"""
        props = {_PROP_IP4_ADDRESS: address}
        self.set_properties(props)

    def get_registered(self):
        """Retrieve whether owner has registered with presence server"""
        return self._registered

    def get_server(self):
        """Retrieve XMPP server hostname (used by the server plugin)"""
        return self._server

    def get_key_hash(self):
        """Retrieve the user's private-key hash (used by the server plugin
        as a password)
        """
        return self._key_hash

    def set_registered(self, registered):
        """Customisation point: handle the registration of the owner"""
        raise RuntimeError("Subclasses must implement")

    def update_avatar(self, tp, new_avatar_token):
        # This should never get called because Owner avatar changes are
        # driven by the Sugar shell, but just in case:
        _logger.warning('GenericOwner.update_avatar() should not be called')


class ShellOwner(GenericOwner):
    """Representation of the local-machine owner using Sugar's Shell

    The ShellOwner uses the Sugar Shell's dbus services to
    register for updates about the user's profile description.
    """
    __gtype_name__ = "ShellOwner"

    _SHELL_SERVICE = "org.laptop.Shell"
    _SHELL_OWNER_INTERFACE = "org.laptop.Shell.Owner"
    _SHELL_PATH = "/org/laptop/Shell"

    def __init__(self, ps, bus):
        """Initialize the ShellOwner instance

        ps -- presenceservice.PresenceService object
        bus -- a connection to the D-Bus session bus

        Retrieves initial property values from the profile
        module.  Loads the buddy icon from file as well.
            XXX note: no error handling on that

        calls GenericOwner.__init__
        """
        server = profile.get_server()
        key_hash = profile.get_private_key_hash()
        registered = profile.get_server_registered()
        key = profile.get_pubkey()
        nick = profile.get_nick_name()
        color = profile.get_color().to_string()

        icon_file = os.path.join(env.get_profile_path(), "buddy-icon.jpg")
        f = open(icon_file, "r")
        icon = f.read()
        f.close()

        GenericOwner.__init__(self, ps, bus,
                'keyid/' + psutils.pubkey_to_keyid(key),
                key=key, nick=nick, color=color, icon=icon, server=server,
                key_hash=key_hash, registered=registered)

        # Ask to get notifications on Owner object property changes in the
        # shell. If it's not currently running, no problem - we'll get the
        # signals when it does run
        for (signal, cb) in (('IconChanged', self._icon_changed_cb),
                             ('ColorChanged', self._color_changed_cb),
                             ('NickChanged', self._nick_changed_cb)):
            self._bus.add_signal_receiver(cb, signal_name=signal,
                dbus_interface=self._SHELL_OWNER_INTERFACE,
                bus_name=self._SHELL_SERVICE,
                path=self._SHELL_PATH)

    def set_registered(self, value):
        """Handle notification that we have been registered"""
        if value:
            profile.set_server_registered()

    def _icon_changed_cb(self, icon):
        """Handle icon change, set property to generate event"""
        self.props.icon = icon

    def _color_changed_cb(self, color):
        """Handle color change, set property to generate event"""
        props = {_PROP_COLOR: color}
        self.set_properties(props)

    def _nick_changed_cb(self, nick):
        """Handle nickname change, set property to generate event"""
        props = {_PROP_NICK: nick}
        self.set_properties(props)

    def _cur_activity_changed_cb(self, activity_id):
        """Handle current-activity change, set property to generate event

        Filters out local activities (those not in self.activites)
        because the network users can't join those activities, so
        the activity_id shared will be None in those cases...
        """
        if not self._activities.has_key(activity_id):
            # This activity is local-only
            activity_id = None
        props = {_PROP_CURACT: activity_id}
        self.set_properties(props)
