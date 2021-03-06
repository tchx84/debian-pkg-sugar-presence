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

import gconf
import os
import logging
try:
    # Python >= 2.5
    from hashlib import md5 as new_md5
except ImportError:
    from md5 import new as new_md5

import gobject
import dbus
import dbus.proxies
import dbus.service
from dbus.gobject_service import ExportedGObject
from telepathy.constants import CONNECTION_STATUS_CONNECTED
from telepathy.interfaces import (CONN_INTERFACE_ALIASING,
                                  CONN_INTERFACE_AVATARS)

from sugar import env
from sugar.profile import get_profile

import psutils
from buddyiconcache import buddy_icon_cache


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'

BUDDY_PATH = "/org/laptop/Sugar/Presence/Buddies/"
_BUDDY_INTERFACE = "org.laptop.Sugar.Presence.Buddy"

_PROP_NICK = "nick"
_PROP_KEY = "key"
_PROP_ICON = "icon"
_PROP_CURACT = "current-activity"
_PROP_COLOR = "color"
_PROP_OWNER = "owner"
_PROP_VALID = "valid"
_PROP_OBJID = 'objid'
_PROP_TAGS = 'tags'

# Will go away soon
_PROP_IP4_ADDRESS = "ip4-address"

_logger = logging.getLogger('s-p-s.buddy')


def _noop(*args, **kwargs):
    pass

def _buddy_icon_save_cb(buf, data):
    data[0] += buf
    return True

def _get_buddy_icon_at_size(icon, maxw, maxh, maxsize):
# FIXME Do not import gtk in the presence service,
# it uses a lot of memory and slow down startup.
#    loader = gtk.gdk.PixbufLoader()
#    loader.write(icon)
#    loader.close()
#    unscaled_pixbuf = loader.get_pixbuf()
#    del loader
#
#    pixbuf = unscaled_pixbuf.scale_simple(maxw, maxh, gtk.gdk.INTERP_BILINEAR)
#    del unscaled_pixbuf
#
#    data = [""]
#    quality = 90
#    img_size = maxsize + 1
#    while img_size > maxsize:
#        data = [""]
#        pixbuf.save_to_callback(_buddy_icon_save_cb, "jpeg",
#                                {"quality":"%d" % quality}, data)
#        quality -= 10
#        img_size = len(data[0])
#    del pixbuf
#
#    if img_size > maxsize:
#        data = [""]
#        raise RuntimeError("could not size image less than %d bytes" % maxsize)
#
#    return str(data[0])

    return ""

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
            # either has, or has tried and failed to get, a color, a nick
            # and a key.
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
        _PROP_KEY          : (str, None, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
        _PROP_ICON         : (object, None, None, gobject.PARAM_READABLE),
        # Must be a unicode object or None
        _PROP_NICK         : (object, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
        _PROP_COLOR        : (str, None, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
        _PROP_CURACT       : (str, None, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
        _PROP_VALID        : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_OWNER        : (bool, None, None, False, gobject.PARAM_READABLE),
        _PROP_OBJID        : (str, None, None, None, gobject.PARAM_READABLE),
        _PROP_IP4_ADDRESS  : (str, None, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
        _PROP_TAGS         : (str, None, None, None,
                              gobject.PARAM_CONSTRUCT_ONLY |
                              gobject.PARAM_READWRITE),
    }

    def __init__(self, bus, object_id, **kwargs):
        """Initialize the Buddy object

        bus -- connection to the D-Bus session bus
        object_id -- the buddy's unique identifier, either based on their
            key-ID or JID
        kwargs -- used to initialize the object's properties

        constructs a DBUS "object path" from the BUDDY_PATH
        and object_id
        """

        self._object_id = object_id
        self._object_path = dbus.ObjectPath(BUDDY_PATH + object_id)

        #: activity ID -> activity
        self._activities = {}
        self._activity_sigids = {}
        #: Telepathy plugin -> (handle, identifier e.g. JID)
        self._handles = {}

        self._awaiting = set(('alias', 'properties'))
        self._owner = False
        self._key = None
        self._icon = ''
        self._current_activity = ''
        self._current_activity_plugin = None
        self._nick = None
        self._color = None
        self._ip4_address = None
        self._tags = None

        _ALLOWED_INIT_PROPS = [_PROP_NICK, _PROP_KEY, _PROP_ICON,
                               _PROP_CURACT, _PROP_COLOR, _PROP_IP4_ADDRESS,
                               _PROP_TAGS]
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

        if icon_data is not None:
            self._icon = str(icon_data)
            self.IconChanged(self._icon)

    def __repr__(self):
        return '<ps.buddy.Buddy %s>' % (self._nick or u'').encode('utf-8')

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
            return not self._awaiting
        elif pspec.name == _PROP_OWNER:
            return self._owner
        elif pspec.name == _PROP_IP4_ADDRESS:
            return self._ip4_address
        elif pspec.name == _PROP_TAGS:
            return self._tags

    def do_set_property(self, pspec, value):
        """Set given property

        pspec -- property specifier with a "name" attribute
        value -- value to set

        emits 'icon-changed' signal on icon setting
        """
        if pspec.name == _PROP_ICON:
            if str(value) != self._icon:
                self._icon = str(value)
                self.IconChanged(self._icon)
        elif pspec.name == _PROP_NICK:
            if value is not None:
                value = unicode(value)
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
        elif pspec.name == _PROP_TAGS:
            self._tags = value

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

        # act as though the buddy signalled ActivitiesChanged([])
        for act in self.get_joined_activities():
            if act.room_details[0] == tp_client:
                act.buddy_apparently_left(self)

        # if the Connection Manager disconnected other than
        # PS stopping it, then we don't have a connection.
        if conn is not None:
            self.TelepathyHandleRemoved(conn.service_name,
                                        conn.object_path, handle)
        # the Owner can't disappear - that would be silly
        if not self._handles and not self._owner:
            self.emit('disappeared')
            # Stop exporting a dbus service
            self.remove_from_connection()

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
        props[_PROP_NICK] = self.props.nick or ''
        props[_PROP_OWNER] = self.props.owner or ''
        props[_PROP_KEY] = self.props.key or ''
        props[_PROP_COLOR] = self.props.color or ''
        props[_PROP_IP4_ADDRESS] = self.props.ip4_address or ''
        props[_PROP_CURACT] = self.props.current_activity or ''
        props[_PROP_TAGS] = self.props.tags or ''
        return props

    def get_identifier_by_plugin(self, plugin):
        """
        :Parameters:
            `plugin` : TelepathyPlugin
                The Telepathy connection
        :Returns: a tuple (Telepathy handle: integer,
            unique identifier: str) or None
        """
        return self._handles.get(plugin)

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
        return ret

    # methods
    def object_path(self):
        """Retrieve our dbus.ObjectPath object"""
        return dbus.ObjectPath(self._object_path)

    def _activity_validity_changed_cb(self, activity, valid):
        """Join or leave the activity when its validity changes"""
        if valid:
            self.JoinedActivity(activity.object_path())
            self.set_properties({_PROP_CURACT: activity.props.id})
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
        if change, generates property-changed
        """
        changed = False
        changed_props = {}
        if _PROP_NICK in properties:
            nick = properties[_PROP_NICK]
            if nick is not None:
                nick = unicode(nick)
            if nick != self._nick:
                self._nick = nick
                changed_props[_PROP_NICK] = nick or u''
                changed = True
        if _PROP_COLOR in properties:
            color = properties[_PROP_COLOR]
            if color != self._color:
                self._color = color
                changed_props[_PROP_COLOR] = color or ''
                changed = True
        if _PROP_CURACT in properties:
            curact = properties[_PROP_CURACT]
            if curact != self._current_activity:
                self._current_activity = curact
                changed_props[_PROP_CURACT] = curact or ''
                changed = True
        if _PROP_IP4_ADDRESS in properties:
            ip4addr = properties[_PROP_IP4_ADDRESS]
            if ip4addr != self._ip4_address:
                self._ip4_address = ip4addr
                changed_props[_PROP_IP4_ADDRESS] = ip4addr or ''
                changed = True
        if _PROP_KEY in properties:
            # don't allow key to be set more than once
            if self._key is None:
                key = properties[_PROP_KEY]
                if key is not None:
                    self._key = key
                    changed_props[_PROP_KEY] = key or ''
                    changed = True
        if _PROP_TAGS in properties:
            tags = properties[_PROP_TAGS]
            if tags != self._tags:
                self._tags = tags
                changed_props[_PROP_TAGS] = tags or ''
                changed = True

        if not changed or not changed_props:
            return

        # Try emitting PropertyChanged before updating validity
        # to avoid leaking a PropertyChanged signal before the buddy is
        # actually valid the first time after creation
        if not self._awaiting:
            dbus_changed = {}
            for key, value in changed_props.items():
                if value:
                    dbus_changed[key] = value
                else:
                    dbus_changed[key] = ""
            self.PropertyChanged(dbus_changed)

            self._property_changed(changed_props)

    def _property_changed(self, changed_props):
        pass

    def update_buddy_properties(self, tp, props):
        """Update the buddy properties (those that come from the GetProperties
        method of the org.laptop.Telepathy.BuddyInfo interface) from the
        given Telepathy connection.

        Other properties, such as 'nick', may not be set via this method.
        """
        self.set_properties(props)
        # If the properties didn't contain the key or color, then we're never
        # going to get one.

        try:
            self._awaiting.remove('properties')
        except KeyError:
            pass
        else:
            if not self._awaiting:
                self.emit('validity-changed', True)

    def update_alias(self, tp, alias):
        """Update the alias from the given Telepathy connection.
        """
        self.set_properties({'nick': alias})
        try:
            self._awaiting.remove('alias')
        except KeyError:
            pass
        else:
            if not self._awaiting:
                self.emit('validity-changed', True)

    def update_current_activity(self, tp, current_activity):
        """Update the current activity from the given Telepathy connection.
        """
        # don't allow an absent current-activity to overwrite a present one
        # unless our current current-activity was advertised by the same
        # Telepathy connection
        if current_activity or self._current_activity_plugin is tp:
            self._current_activity_plugin = tp
            gobject.timeout_add(500, 
                lambda: self.set_properties(
                    {_PROP_CURACT: current_activity}))

    def update_avatar(self, tp, new_avatar_token, icon=None, mime_type=None):
        """Handle update of the avatar"""

        # FIXME: Avatars have been disabled for Trial-2 due to performance
        # issues in the avatar cache. Revisit this afterwards
        return

        conn = tp.get_connection()
        handle, identifier = self._handles[tp]

        if CONN_INTERFACE_AVATARS not in conn:
            return

        if icon is None:
            icon = buddy_icon_cache.get_icon(conn.object_path, identifier,
                                             new_avatar_token)
        else:
            buddy_icon_cache.store_icon(conn.object_path, identifier,
                                        new_avatar_token, icon)

        if icon is None:
            # this was AvatarUpdated not AvatarRetrieved, and then we got a
            # cache miss - request an AvatarRetrieved signal so we can get the
            # actual icon
            conn[CONN_INTERFACE_AVATARS].RequestAvatars([handle],
                                                        ignore_reply=True)
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
        self._key_hash = kwargs.pop("key_hash", None)

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

    def remove_owner_activity(self, tp, activity_id):
        # FIXME: this probably duplicates something else (_activities?)
        # but for now I'll keep the same duplication as before.
        # Equivalent code used to be in ServerPlugin.
        id_to_act = self._activities_by_connection.setdefault(tp, {})
        del id_to_act[activity_id]

        self._set_self_activities(tp)
        if self._current_activity == activity_id:
            self.set_properties({_PROP_CURACT: None})

    def _set_self_activities(self, tp):
        """Forward set of joined activities to network

        uses SetActivities on BuddyInfo channel
        """
        pass

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
                cur_activity = ""
                cur_activity_handle = 0

    def _set_self_alias(self, tp):
        pass

        # Hack so we can use this as a timeout handler
        return False

    def set_properties_before_connect(self, tp):
        self._set_self_olpc_properties(tp, connected=False)

    def _set_self_olpc_properties(self, tp, connected=True):
        conn = tp.get_connection()
        # FIXME: omit color/key/ip4-address if None?

        props = dbus.Dictionary({
            'color': self._color or '',
            'key': dbus.ByteArray(self._key or ''),
            'ip4-address': self._ip4_address or '',
            'tags': self._tags or '',
            }, signature='sv')

        # FIXME: clarify whether we're meant to support random extra properties
        # (Salut doesn't)
        if tp._PROTOCOL == 'local-xmpp':
            del props['tags']

        # Hack so we can use this as a timeout handler
        return False

    def add_telepathy_handle(self, tp_client, handle, uid):
        Buddy.add_telepathy_handle(self, tp_client, handle, uid)
        self._activities_by_connection.setdefault(tp_client, {})

        self._set_self_olpc_properties(tp_client)
        self._set_self_alias(tp_client)
        # Hack; send twice to make sure the server gets it
        #gobject.timeout_add(1000, lambda: self._set_self_alias(tp_client))

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
        # FIXME: Avatars have been disabled for Trial-2 due to performance
        # issues in the avatar cache. Revisit this afterwards
        return

    def _property_changed(self, changed_props):
        for tp in self._handles.iterkeys():

            if changed_props.has_key("current-activity"):
                self._set_self_current_activity(tp)

            if changed_props.has_key("nick"):
                self._set_self_alias(tp)
                # Hack; send twice to make sure the server gets it
                gobject.timeout_add(1000, lambda: self._set_self_alias(tp))

            if (changed_props.has_key("color") or
                changed_props.has_key("ip4-address") or
                changed_props.has_key("tags")):
                if tp.status == CONNECTION_STATUS_CONNECTED:
                    self._set_self_olpc_properties(tp)

    def _ip4_address_changed_cb(self, monitor, address, iface):
        """Handle IPv4 address change, set property to generate event"""
        props = {_PROP_IP4_ADDRESS: address}
        self.set_properties(props)

    def get_server(self):
        """Retrieve XMPP server hostname (used by the server plugin)"""
        client = gconf.client_get_default()    
        server = client.get_string("/desktop/sugar/collaboration/jabber_server")
        return server

    def get_key_hash(self):
        """Retrieve the user's private-key hash (used by the server plugin
        as a password)
        """
        return self._key_hash

    def update_avatar(self, tp, new_avatar_token, icon=None, mime_type=None):
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
        client = gconf.client_get_default()    
        profile = get_profile()

        key_hash = profile.privkey_hash
        key = profile.pubkey

        color = client.get_string("/desktop/sugar/user/color")
        tags = client.get_string("/desktop/sugar/user/tags")
        nick = client.get_string("/desktop/sugar/user/nick")

        if not isinstance(nick, unicode):
            nick = unicode(nick, 'utf-8')

        GenericOwner.__init__(self, ps, bus,
                'keyid/' + psutils.pubkey_to_keyid(key),
                key=key, nick=nick, color=color, icon=None, key_hash=key_hash,
                tags=tags)

        # Ask to get notifications on Owner object property changes in the
        # shell. If it's not currently running, no problem - we'll get the
        # signals when it does run
        for (signal, cb) in (('IconChanged', self._icon_changed_cb),
                             ('ColorChanged', self._color_changed_cb),
                             ('NickChanged', self._nick_changed_cb),
                             ('TagsChanged', self._tags_changed_cb),
                             ('CurrentActivityChanged',
                              self._cur_activity_changed_cb)):
            self._bus.add_signal_receiver(cb, signal_name=signal,
                dbus_interface=self._SHELL_OWNER_INTERFACE,
                bus_name=self._SHELL_SERVICE,
                path=self._SHELL_PATH)

        # we already know our own nick, color, key
        self._awaiting = None

    def _icon_changed_cb(self, icon):
        """Handle icon change, set property to generate event"""
        icon = str(icon)
        if icon != self._icon:
            self._icon = icon
            self.IconChanged(icon)

    def _color_changed_cb(self, color):
        """Handle color change, set property to generate event"""
        props = {_PROP_COLOR: color}
        self.set_properties(props)

    def _tags_changed_cb(self, tags):
        """Handle tags change, set property to generate event"""
        props = {_PROP_TAGS: tags}
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
            print 'Local only activity'
            # This activity is local-only
            activity_id = None
        props = {_PROP_CURACT: activity_id}
        self.set_properties(props)
