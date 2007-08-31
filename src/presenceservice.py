# Copyright (C) 2007, Red Hat, Inc.
# Copyright (C) 2007 Collabora Ltd. <http://www.collabora.co.uk/>
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
from itertools import izip
from os import environ
from weakref import WeakValueDictionary

import dbus
import dbus.service
import gobject
from dbus.gobject_service import ExportedGObject
from dbus.mainloop.glib import DBusGMainLoop
from telepathy.client import ManagerRegistry, Connection
from telepathy.interfaces import (CONN_MGR_INTERFACE, CONN_INTERFACE,
    CONN_INTERFACE_AVATARS, CONN_INTERFACE_ALIASING)
from telepathy.constants import (CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED)

from sugar import util

from server_plugin import ServerPlugin
from linklocal_plugin import LinkLocalPlugin
from buddy import Buddy, ShellOwner, BUDDY_PATH
from activity import Activity
from psutils import pubkey_to_keyid, NotFoundError, PRESENCE_INTERFACE

CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'
CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'

_PRESENCE_SERVICE = "org.laptop.Sugar.Presence"
_PRESENCE_PATH = "/org/laptop/Sugar/Presence"


_logger = logging.getLogger('s-p-s.presenceservice')

class PresenceService(ExportedGObject):
    __gtype_name__ = "PresenceService"

    def _create_owner(self):
        # Overridden by TestPresenceService
        return ShellOwner(self, self._session_bus)

    def __init__(self):
        self._next_object_id = 0

        # all Buddy objects
        # identifier -> Buddy, GC'd when no more refs exist
        self._buddies = WeakValueDictionary()

        # the online buddies for whom we know the full public key
        # base64 public key -> Buddy
        self._buddies_by_pubkey = {}

        # The online buddies (those who're available via some CM)
        # TP plugin -> (handle -> Buddy)
        self._handles_buddies = {}

        # activity id -> Activity
        self._activities_by_id = {}
        #: Tp plugin -> (handle -> Activity)
        self._activities_by_handle = {}

        #: Connection -> list of SignalMatch
        self._conn_matches = {}

        self._session_bus = dbus.SessionBus()
        self._session_bus.add_signal_receiver(self._connection_disconnected_cb,
                signal_name="Disconnected",
                dbus_interface="org.freedesktop.DBus")

        # Create the Owner object
        self._owner = self._create_owner()
        key = self._owner.props.key
        keyid = pubkey_to_keyid(key)
        self._buddies['keyid/' + keyid] = self._owner
        self._buddies_by_pubkey[key] = self._owner

        self._registry = ManagerRegistry()
        self._registry.LoadManagers()

        # Set up the Telepathy plugins
        self._plugins = []
        debug_flags = set(environ.get('PRESENCE_SERVICE_DEBUG', '').split(','))
        _logger.debug('Debug flags: %r', debug_flags)
        if 'disable-gabble' in debug_flags:
            self._server_plugin = None
        else:
            server = self._owner.get_server()
            if server and len(server):
                self._server_plugin = ServerPlugin(self._registry, self._owner)
                self._plugins.append(self._server_plugin)
            else:
                self._server_plugin = None
        if 'disable-salut' in debug_flags:
            self._ll_plugin = None
        else:
            self._ll_plugin = LinkLocalPlugin(self._registry, self._owner)
            self._plugins.append(self._ll_plugin)
        self._connected_plugins = set()

        for tp in self._plugins:
            self._handles_buddies[tp] = {}
            self._activities_by_handle[tp] = {}

            tp.connect('status', self._tp_status_cb)
            tp.connect('contacts-online', self._contacts_online)
            tp.connect('contacts-offline', self._contacts_offline)
            tp.connect('activity-invitation',
                                        self._activity_invitation)
            tp.connect('private-invitation',
                                        self._private_invitation)
            tp.start()

        self._contacts_online_queue = []

        ExportedGObject.__init__(self, self._session_bus, _PRESENCE_PATH)

        # for activation to work in a race-free way, we should really
        # export the bus name only after we export our initial object;
        # so this comes after the parent __init__
        self._bus_name = dbus.service.BusName(_PRESENCE_SERVICE,
                                              bus=self._session_bus)

    @property
    def owner(self):
        return self._owner

    def _connection_disconnected_cb(self, foo=None):
        """Log event when D-Bus kicks us off the bus for some reason"""
        _logger.debug("Disconnected from session bus!!!")

    def _tp_status_cb(self, plugin, status, reason):
        if status == CONNECTION_STATUS_CONNECTED:
            self._tp_connected(plugin)
        else:
            self._tp_disconnected(plugin)

    def _tp_connected(self, tp):
        self._connected_plugins.add(tp)
        self._handles_buddies[tp][tp.self_handle] = self._owner
        self._owner.add_telepathy_handle(tp, tp.self_handle,
                                         tp.self_identifier)

        conn = tp.get_connection()

        self._conn_matches[conn] = []

        if CONN_INTERFACE_ACTIVITY_PROPERTIES in conn:
            def activity_properties_changed(room, properties):
                self._activity_properties_changed(tp, room, properties)
            m = conn[CONN_INTERFACE_ACTIVITY_PROPERTIES].connect_to_signal(
                    'ActivityPropertiesChanged',
                    activity_properties_changed)
            self._conn_matches[conn].append(m)
        else:
            _logger.warning('Connection %s does not support OLPC activity '
                            'properties', conn.object_path)

        if CONN_INTERFACE_BUDDY_INFO in conn:
            def buddy_activities_changed(contact, activities):
                _logger.debug('ActivitiesChanged on %s: (%u, %r)', tp,
                              contact, activities)
                self._buddy_activities_changed(tp, contact, activities)
            m = conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                    'ActivitiesChanged', buddy_activities_changed)
            self._conn_matches[conn].append(m)

            def buddy_properties_changed(contact, properties):
                buddy = self._handles_buddies[tp].get(contact)
                if buddy is not None and buddy is not self._owner:
                    buddy.update_buddy_properties(tp, properties)
            m = conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                'PropertiesChanged', buddy_properties_changed)
            self._conn_matches[conn].append(m)

            def buddy_curact_changed(contact, act_id, room):
                if (act_id == '' or not util.validate_activity_id(act_id) or
                    room == 0):
                    act_id = ''
                    room = 0
                buddy = self._handles_buddies[tp].get(contact)
                if buddy is not None and buddy is not self._owner:
                    buddy.update_current_activity(tp, act_id)
                # FIXME: do something useful with the room handle?
            m = conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                'CurrentActivityChanged', buddy_curact_changed)
            self._conn_matches[conn].append(m)
        else:
            _logger.warning('Connection %s does not support OLPC buddy info',
                            conn.object_path)

        if 1:
            # FIXME: Avatars have been disabled for Trial-2 due to performance
            # issues in the avatar cache. Revisit this afterwards
            pass
        elif CONN_INTERFACE_AVATARS in conn:
            def avatar_retrieved(contact, avatar_token, avatar, mime_type):
                self._avatar_updated(tp, contact, avatar_token, avatar,
                                     mime_type)
            m = conn[CONN_INTERFACE_AVATARS].connect_to_signal(
                    'AvatarRetrieved', avatar_retrieved)
            self._conn_matches[conn].append(m)

            def avatar_updated(contact, avatar_token):
                self._avatar_updated(tp, contact, avatar_token)
            m = conn[CONN_INTERFACE_AVATARS].connect_to_signal('AvatarUpdated',
                    avatar_updated)
            self._conn_matches[conn].append(m)
        else:
            _logger.warning('Connection %s does not support avatars',
                            conn.object_path)

        if CONN_INTERFACE_ALIASING in conn:
            def aliases_changed(aliases):
                for contact, alias in aliases:
                    buddy = self._handles_buddies[tp].get(contact)
                    if buddy is not None and buddy is not self._owner:
                        buddy.update_alias(tp, alias)
            m = conn[CONN_INTERFACE_ALIASING].connect_to_signal(
                    'AliasesChanged', aliases_changed)
            self._conn_matches[conn].append(m)
        else:
            _logger.warning('Connection %s does not support aliasing',
                            conn.object_path)

    def _tp_disconnected(self, tp):
        self._connected_plugins.discard(tp)
        if tp.self_handle is not None:
            self._handles_buddies.setdefault(tp, {}).pop(
                    tp.self_handle, None)
        self._owner.remove_telepathy_handle(tp)

        conn = tp.get_connection()

        matches = self._conn_matches.get(conn)
        try:
            del self._conn_matches[conn]
        except KeyError:
            pass
        if matches is not None:
            for match in matches:
                match.remove()

    def get_buddy_by_path(self, path):
        """Get the Buddy object corresponding to an object-path, or None.

        :Parameters:
            path : dbus.ObjectPath
                The object-path of a buddy
        :Returns: a Buddy object or None
        """
        if not path.startswith(BUDDY_PATH):
            return None
        return self._buddies.get(path[len(BUDDY_PATH):])

    def get_buddy(self, objid):
        buddy = self._buddies.get(objid)
        if buddy is None:
            _logger.debug('Creating new buddy at .../%s', objid)
            # we don't know yet this buddy
            buddy = Buddy(self._session_bus, objid)
            buddy.connect("validity-changed", self._buddy_validity_changed_cb)
            buddy.connect("disappeared", self._buddy_disappeared_cb)
            self._buddies[objid] = buddy
        return buddy

    def _contacts_online(self, tp, objids, handles, identifiers):
        # we'll iterate over handles many times, so make sure that will
        # work
        if not isinstance(handles, (list, tuple)):
            handles = tuple(handles)

        for objid, handle, identifier in izip(objids, handles, identifiers):
            _logger.debug('Handle %u, .../%s is now online', handle, objid)
            buddy = self.get_buddy(objid)

            self._handles_buddies[tp][handle] = buddy
            # Store the handle of the buddy for this CM. This doesn't
            # fetch anything over D-Bus, to avoid reaching the pending-call
            # limit.
            buddy.add_telepathy_handle(tp, handle, identifier)

        conn = tp.get_connection()

        if not self._contacts_online_queue:
            gobject.idle_add(self._run_contacts_online_queue)

        def handle_error(e, when):
            gobject.idle_add(self._run_contacts_online_queue)
            _logger.warning('Error %s: %s', when, e)

        if CONN_INTERFACE_ALIASING in conn:
            def got_aliases(aliases):
                gobject.idle_add(self._run_contacts_online_queue)
                for contact, alias in izip(handles, aliases):
                    buddy = self._handles_buddies[tp].get(contact)
                    if buddy is not None and buddy is not self._owner:
                        buddy.update_alias(tp, alias)
            def request_aliases():
                try:
                    conn[CONN_INTERFACE_ALIASING].RequestAliases(handles,
                        reply_handler=got_aliases,
                        error_handler=lambda e:
                            handle_error(e, 'fetching aliases'))
                except Exception, e:
                    gobject.idle_add(self._run_contacts_online_queue)
                    handle_error(e, 'fetching aliases')
            self._contacts_online_queue.append(request_aliases)

        for handle in handles:
            self._queue_contact_online(tp, handle)

        if 1:
            # FIXME: Avatars have been disabled for Trial-2 due to performance
            # issues in the avatar cache. Revisit this afterwards
            pass
        elif CONN_INTERFACE_AVATARS in conn:
            def got_avatar_tokens(tokens):
                gobject.idle_add(self._run_contacts_online_queue)
                for contact, token in izip(handles, tokens):
                    self._avatar_updated(tp, contact, token)
            def get_avatar_tokens():
                try:
                    conn[CONN_INTERFACE_AVATARS].GetAvatarTokens(handles,
                        reply_handler=got_avatar_tokens,
                        error_handler=lambda e:
                            handle_error(e, 'fetching avatar tokens'))
                except Exception, e:
                    gobject.idle_add(self._run_contacts_online_queue)
                    handle_error(e, 'fetching avatar tokens')
            self._contacts_online_queue.append(get_avatar_tokens)

    def _queue_contact_online(self, tp, contact):
        conn = tp.get_connection()

        if CONN_INTERFACE_BUDDY_INFO in conn:
            def handle_error(e, when):
                gobject.idle_add(self._run_contacts_online_queue)
                buddy = self._handles_buddies[tp].get(contact)
                if buddy is not None:
                    buddy = buddy.props.objid
                _logger.warning('Error %s for handle %u %s: %s', when,
                                contact, buddy, e)
            def got_properties(props):
                gobject.idle_add(self._run_contacts_online_queue)
                buddy = self._handles_buddies[tp].get(contact)
                if buddy is not None and buddy is not self._owner:
                    buddy.update_buddy_properties(tp, props)
            def get_properties():
                try:
                    conn[CONN_INTERFACE_BUDDY_INFO].GetProperties(contact,
                        byte_arrays=True, reply_handler=got_properties,
                        error_handler=lambda e:
                            handle_error(e, 'fetching buddy properties'))
                except Exception, e:
                    gobject.idle_add(self._run_contacts_online_queue)
                    handle_error(e, 'fetching buddy properties')
            def got_current_activity(current_activity, room):
                gobject.idle_add(self._run_contacts_online_queue)
                buddy = self._handles_buddies[tp].get(contact)
                if buddy is not None and buddy is not self._owner:
                    buddy.update_current_activity(tp, current_activity)
            def get_current_activity():
                try:
                    conn[CONN_INTERFACE_BUDDY_INFO].GetCurrentActivity(contact,
                        reply_handler=got_current_activity,
                        error_handler=lambda e:
                            handle_error(e, 'fetching current activity'))
                except Exception, e:
                    gobject.idle_add(self._run_contacts_online_queue)
                    handle_error(e, 'fetching current activity')
            def got_activities(activities):
                gobject.idle_add(self._run_contacts_online_queue)
                _logger.debug('GetActivities() returned on %s contact %u: %r',
                              tp, contact, activities)
                self._buddy_activities_changed(tp, contact, activities)
            def get_activities():
                try:
                    conn[CONN_INTERFACE_BUDDY_INFO].GetActivities(contact,
                        reply_handler=got_activities,
                        error_handler=lambda e:
                            handle_error(e, 'fetching activities'))
                except Exception, e:
                    gobject.idle_add(self._run_contacts_online_queue)
                    handle_error(e, 'fetching activities')

            self._contacts_online_queue.append(get_properties)
            self._contacts_online_queue.append(get_current_activity)
            self._contacts_online_queue.append(get_activities)

    def _run_contacts_online_queue(self):
        try:
            callback = self._contacts_online_queue.pop(0)
        except IndexError:
            pass
        else:
            callback()
        return False

    def _buddy_validity_changed_cb(self, buddy, valid):
        if valid:
            self.BuddyAppeared(buddy.object_path())
            if buddy.props.key is not None:
                self._buddies_by_pubkey[buddy.props.key] = buddy
            _logger.debug("New Buddy: %s (%s)", buddy.props.nick,
                          buddy.props.color)
        else:
            self.BuddyDisappeared(buddy.object_path())
            self._buddies_by_pubkey.pop(buddy.props.key, None)
            _logger.debug("Buddy left: %s (%s)", buddy.props.nick,
                          buddy.props.color)

    def _buddy_disappeared_cb(self, buddy):
        if buddy.props.valid:
            self._buddy_validity_changed_cb(buddy, False)
        self._buddies.pop(buddy.props.objid, None)

    def _contacts_offline(self, tp, handles):
        for handle in handles:
            buddy = self._handles_buddies[tp].pop(handle, None)
            # the handle of the buddy for this CM is not valid anymore
            # (this might trigger _buddy_disappeared_cb if they are not
            # visible via any CM)
            if buddy is not None:
                buddy.remove_telepathy_handle(tp)

    def _get_next_object_id(self):
        """Increment and return the object ID counter."""
        self._next_object_id = self._next_object_id + 1
        return self._next_object_id

    def _avatar_updated(self, tp, handle, new_avatar_token, avatar=None,
                        mime_type=None):
        buddy = self._handles_buddies[tp].get(handle)
        if buddy is not None and buddy is not self._owner:
            _logger.debug("Buddy %s icon updated" % buddy.props.nick)
            buddy.update_avatar(tp, new_avatar_token, avatar, mime_type)

    def _new_activity(self, activity_id, tp, room):
        try:
            objid = self._get_next_object_id()
            activity = Activity(self._session_bus, objid, self, tp, room,
                                id=activity_id)
        except Exception:
            # FIXME: catching bare Exception considered harmful
            _logger.debug("Invalid activity:", exc_info=1)
            try:
                del self._activities_by_handle[tp][room]
            except KeyError:
                pass
            return None

        activity.connect("validity-changed",
                         self._activity_validity_changed_cb)
        activity.connect("disappeared", self._activity_disappeared_cb)
        self._activities_by_id[activity_id] = activity
        self._activities_by_handle[tp][room] = activity
        return activity

    def _activity_disappeared_cb(self, activity):
        _logger.debug("activity %s disappeared" % activity.props.id)

        self.ActivityDisappeared(activity.object_path())
        try:
            del self._activities_by_id[activity.props.id]
        except KeyError:
            pass
        tp, room = activity.room_details
        try:
            del self._activities_by_handle[tp][room]
        except KeyError:
            pass

    def _buddy_activities_changed(self, tp, contact_handle, activities):
        activities = dict(activities)
        buddies = self._handles_buddies[tp]
        buddy = buddies.get(contact_handle)

        if buddy is self._owner:
            # ignore network events for Owner activity changes since those
            # are handled locally
            return

        if not buddy:
            # We don't know this buddy
            # FIXME: What should we do here?
            # FIXME: Do we need to check if the buddy is valid or something?
            _logger.debug("contact_activities_changed: buddy unknown")
            return

        old_activities = set()
        for activity in buddy.get_joined_activities():
            if activity.room_details[0] == tp:
                old_activities.add(activity.props.id)

        new_activities = set(activities.iterkeys())

        activities_joined = new_activities - old_activities

        for act in activities_joined:
            room_handle = activities[act]
            _logger.debug("Handle %s claims to have joined activity %s",
                          contact_handle, act)
            activity = self._activities_by_id.get(act)
            if activity is None:
                # new activity, can fail
                _logger.debug('No activity object for %s, creating one', act)
                activity = self._new_activity(act, tp, room_handle)

            if activity is None:
                _logger.debug('Failed to create activity object for %s', act)
            else:
                activity.buddy_apparently_joined(buddy)

        activities_left = old_activities - new_activities
        for act in activities_left:
            _logger.debug("Handle %s claims to have left activity %s",
                          contact_handle, act)
            activity = self._activities_by_id.get(act)
            if activity is None:
                # don't bother creating an Activity just so someone can leave
                continue

            activity.buddy_apparently_left(buddy)

    def _activity_invitation(self, tp, channel, act_handle, actor, message):
        activity = self._activities_by_handle[tp].get(act_handle)
        if activity is None:
            # FIXME: we should synthesize an activity somehow, for the case of
            # an invite to a non-activity
            _logger.debug('Invited to unknown activity: handle %u on %s, '
                          'ignoring', act_handle, tp)
        elif actor == 0:
            # don't know who invited us? not much we can do about that, then
            _logger.debug('Invited to activity by unknown contact, ignoring')
        else:
            buddy = self.map_handles_to_buddies(tp, channel, (actor,))[actor]
            self.ActivityInvitation(activity.object_path(),
                                    buddy.object_path(), message)

    def _private_invitation(self, tp, chan_path):
        conn = tp.get_connection()
        self.PrivateInvitation(str(conn.service_name), conn.object_path,
                               chan_path)

    @dbus.service.signal(PRESENCE_INTERFACE, signature="o")
    def ActivityAppeared(self, activity):
        pass

    @dbus.service.signal(PRESENCE_INTERFACE, signature="o")
    def ActivityDisappeared(self, activity):
        pass

    @dbus.service.signal(PRESENCE_INTERFACE, signature="o")
    def BuddyAppeared(self, buddy):
        pass

    @dbus.service.signal(PRESENCE_INTERFACE, signature="o")
    def BuddyDisappeared(self, buddy):
        pass

    @dbus.service.signal(PRESENCE_INTERFACE, signature="oos")
    def ActivityInvitation(self, activity, buddy, message):
        pass

    @dbus.service.signal(PRESENCE_INTERFACE, signature="soo")
    def PrivateInvitation(self, bus_name, connection, channel):
        pass

    @dbus.service.method(PRESENCE_INTERFACE, in_signature='',
                         out_signature="ao")
    def GetActivities(self):
        ret = []
        for act in self._activities_by_id.values():
            if act.props.valid:
                ret.append(act.object_path())
        return ret

    @dbus.service.method(PRESENCE_INTERFACE, in_signature="s",
                         out_signature="o")
    def GetActivityById(self, actid):
        act = self._activities_by_id.get(actid, None)
        if not act or not act.props.valid:
            raise NotFoundError("The activity was not found.")
        return act.object_path()

    @dbus.service.method(PRESENCE_INTERFACE, in_signature='',
                         out_signature="ao")
    def GetBuddies(self):
        # in the presence of an out_signature, dbus-python will convert
        # this set into an Array automatically (because it's iterable),
        # so it's easy to use for uniquification (we want to avoid returning
        # buddies who're visible on both Salut and Gabble twice)

        # always include myself even if I have no handles
        ret = set((self._owner,))

        for handles_buddies in self._handles_buddies.itervalues():
            for buddy in handles_buddies.itervalues():
                if buddy.props.valid:
                    ret.add(buddy)
        return ret

    @dbus.service.method(PRESENCE_INTERFACE,
                         in_signature="ay", out_signature="o",
                         byte_arrays=True)
    def GetBuddyByPublicKey(self, key):
        buddy = self._buddies_by_pubkey.get(key)
        if buddy is not None:
            if buddy.props.valid:
                return buddy.object_path()
        keyid = pubkey_to_keyid(key)
        buddy = self._buddies.get('keyid/' + keyid)
        if buddy is not None:
            if buddy.props.valid:
                return buddy.object_path()
        raise NotFoundError("The buddy was not found.")

    @dbus.service.method(PRESENCE_INTERFACE, in_signature='sou',
                         out_signature='o')
    def GetBuddyByTelepathyHandle(self, tp_conn_name, tp_conn_path, handle):
        """Get the buddy corresponding to a Telepathy handle.

        :Parameters:
            `tp_conn_name` : str
                The well-known bus name of a Telepathy connection
            `tp_conn_path` : dbus.ObjectPath
                The object path of the Telepathy connection
            `handle` : int or long
                The handle of a Telepathy contact on that connection,
                of type HANDLE_TYPE_CONTACT. This may not be a
                channel-specific handle.
        :Returns: the object path of a Buddy
        :Raises NotFoundError: if the buddy is not found.
        """
        for tp, handles in self._handles_buddies.iteritems():
            conn = tp.get_connection()
            if conn is None:
                continue
            if (conn.service_name == tp_conn_name
                and conn.object_path == tp_conn_path):
                buddy = handles.get(handle)
                if buddy is not None and buddy.props.valid:
                        return buddy.object_path()
                # either the handle is invalid, or we don't have a Buddy
                # object for that buddy because we don't have all their
                # details yet
                raise NotFoundError("The buddy %u was not found on the "
                                    "connection to %s:%s"
                                    % (handle, tp_conn_name, tp_conn_path))
        raise NotFoundError("The buddy %u was not found: we have no "
                            "connection to %s:%s" % (handle, tp_conn_name,
                                                     tp_conn_path))

    def map_handles_to_buddies(self, tp, tp_chan, handles, create=True):
        """

        :Parameters:
            `tp` : Telepathy plugin
                The server or link-local plugin
            `tp_chan` : telepathy.client.Channel or None
                If not None, the channel in which these handles are
                channel-specific
            `handles` : iterable over int or long
                The handles to be mapped to Buddy objects
            `create` : bool
                If true (default), if a corresponding `Buddy` object is not
                found, create one.
        :Returns:
            A dict mapping handles from `handles` to `Buddy` objects.
            If `create` is true, the dict's keys will be exactly the
            items of `handles` in some order. If `create` is false,
            the dict will contain no entry for handles for which no
            `Buddy` is already available.
        :Raises LookupError: if `tp` is not a plugin attached to this PS.
        """
        handle_to_buddy = self._handles_buddies[tp]

        ret = {}
        missing = []
        for handle in handles:
            buddy = handle_to_buddy.get(handle)
            if buddy is None:
                missing.append(handle)
            else:
                ret[handle] = buddy

        if missing and create:
            handle_to_objid = tp.identify_contacts(tp_chan, missing)
            for handle, objid in handle_to_objid.iteritems():
                buddy = self.get_buddy(objid)
                ret[handle] = buddy
                if tp_chan is None:
                    handle_to_buddy[handle] = buddy
        return ret

    @dbus.service.method(PRESENCE_INTERFACE,
                         in_signature='', out_signature="o")
    def GetOwner(self):
        if not self._owner:
            raise NotFoundError("The owner was not found.")
        else:
            return self._owner.object_path()

    @dbus.service.method(PRESENCE_INTERFACE, in_signature="sssa{sv}",
            out_signature="o", async_callbacks=('async_cb', 'async_err_cb'))
    def ShareActivity(self, actid, atype, name, properties, async_cb,
                      async_err_cb):
        # FIXME: this makes all activities start off public.
        # Once mutable properties have landed in sugar.presence, we should
        # change the default to private=True.
        _logger.debug('ShareActivity(actid=%r, atype=%r, name=%r, '
                      'properties=%r)', actid, atype, name, properties)
        self._share_activity(actid, atype, name, properties, False,
                             async_cb, async_err_cb)

    def _get_preferred_plugin(self):
        for tp in self._plugins:
            if tp in self._connected_plugins:
                return tp
        return None

    @dbus.service.method(PRESENCE_INTERFACE,
                         in_signature='', out_signature="so")
    def GetPreferredConnection(self):
        tp = self._get_preferred_plugin()
        if tp is None:
            raise NotFoundError('No connection is available')
        conn = tp.get_connection()
        return str(conn.service_name), conn.object_path

    def cleanup(self):
        for tp in self._handles_buddies:
            tp.cleanup()

    def _share_activity(self, actid, atype, name, properties, private,
                        async_cb, async_err_cb):
        """Create the shared Activity.

        actid -- XXX
        atype -- XXX
        name -- XXX
        properties -- XXX
        private -- bool: True for by-invitation-only sharing,
            False for publicly advertised sharing
        async_cb -- function: Callback for success
        async_err_cb -- function: Callback for failure
        """
        objid = self._get_next_object_id()
        # XXX: is the preferred Telepathy plugin always the right way to
        # share the activity?
        color = self._owner.props.color
        activity = Activity(self._session_bus, objid, self,
                            self._get_preferred_plugin(), 0,
                            id=actid, type=atype,
                            name=name, color=color, local=True,
                            private=private)
        activity.connect("validity-changed",
                         self._activity_validity_changed_cb)
        activity.connect("disappeared", self._activity_disappeared_cb)
        self._activities_by_id[actid] = activity

        def activity_shared():
            tp, room = activity.room_details
            self._activities_by_handle[tp][room] = activity
            async_cb(activity.object_path())

        activity.join(activity_shared, async_err_cb, True, private)

        # local activities are valid at creation by definition, but we can't
        # connect to the activity's validity-changed signal until its already
        # issued the signal, which happens in the activity's constructor
        # for local activities.
        self._activity_validity_changed_cb(activity, activity.props.valid)

    def _activity_validity_changed_cb(self, activity, valid):
        if valid:
            self.ActivityAppeared(activity.object_path())
            _logger.debug("New Activity: %s (%s)", activity.props.name,
                          activity.props.id)
        else:
            self.ActivityDisappeared(activity.object_path())
            _logger.debug("Activity disappeared: %s (%s)",
                          activity.props.name, activity.props.id)

    def _activity_properties_changed(self, tp, act_handle, props):
        activity = self._activities_by_handle[tp].get(act_handle)
        if activity is None:
            # FIXME: synthesize an activity
            pass
        else:
            activity.set_properties(props)


def main(test_num=0, randomize=False):
    loop = gobject.MainLoop()
    DBusGMainLoop(set_as_default=True)

    if dbus.version < (0, 82, 0):
        _logger.error('dbus-python %s is too old (0.82.0 is required). '
                      'The PS is unlikely to work correctly.',
                      dbus.__version__)

    if test_num > 0:
        from pstest import TestPresenceService
        ps = TestPresenceService(test_num, randomize)
    else:
        ps = PresenceService()

    try:
        loop.run()
    except KeyboardInterrupt:
        ps.cleanup()
        _logger.debug('Ctrl+C pressed, exiting...')

if __name__ == "__main__":
    main()
