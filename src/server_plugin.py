"""Telepathy-python presence server interface/implementation plugin"""
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

# Standard library
import logging
import os
import sys
from string import hexdigits

# Other libraries
import dbus
import gobject
import gtk
from telepathy.client import (ConnectionManager, ManagerRegistry, Connection,
    Channel)
from telepathy.interfaces import (CONN_MGR_INTERFACE, CONN_INTERFACE,
    CHANNEL_TYPE_CONTACT_LIST, CHANNEL_INTERFACE_GROUP,
    CONN_INTERFACE_ALIASING, CONN_INTERFACE_AVATARS, CONN_INTERFACE_PRESENCE,
    CHANNEL_TYPE_TEXT, CHANNEL_TYPE_STREAMED_MEDIA, PROPERTIES_INTERFACE)
from telepathy.constants import (HANDLE_TYPE_CONTACT,
    HANDLE_TYPE_LIST, HANDLE_TYPE_CONTACT, HANDLE_TYPE_ROOM,
    CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_CONNECTING,
    CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED,
    CONNECTION_STATUS_REASON_NONE_SPECIFIED,
    CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES,
    PROPERTY_FLAG_WRITE)
from sugar import util

# Presence Service local modules
import psutils
from buddyiconcache import buddy_icon_cache


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'
CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'

_PROTOCOL = "jabber"
_OBJ_PATH_PREFIX = "/org/freedesktop/Telepathy/Connection/gabble/jabber/"

_logger = logging.getLogger('s-p-s.server_plugin')

_RECONNECT_TIMEOUT = 5000


class ServerPlugin(gobject.GObject):
    """Telepathy-python-based presence server interface

    The ServerPlugin instance translates network events from
    Telepathy Python into GObject events.  It provides direct
    python calls to perform the required network operations
    to implement the PresenceService.
    """
    __gsignals__ = {
        'contact-online':
            # Contact has come online and we've discovered all their buddy
            # properties.
            # args:
            #   contact identification (based on key ID or JID): str
            #   contact handle: int or long
            #   dict {name: str => property: object}
            (gobject.SIGNAL_RUN_FIRST, None, [str, object, object]),
        'contact-offline':
            # Contact has gone offline.
            # args: contact handle
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'status':
            # Connection status changed.
            # args: status, reason as for Telepathy StatusChanged
            (gobject.SIGNAL_RUN_FIRST, None, [int, int]),
        'avatar-updated':
            # Contact's avatar has changed
            # args:
            #   contact handle: int
            #   icon data: str
            (gobject.SIGNAL_RUN_FIRST, None, [object, object]),
        'buddy-properties-changed':
            # OLPC buddy properties changed; as for PropertiesChanged
            # args:
            #   contact handle: int
            #   properties: dict {name: str => property: object}
            # FIXME: are these all the properties or just those that changed?
            (gobject.SIGNAL_RUN_FIRST, None, [object, object]),
        'buddy-activities-changed':
            # OLPC activities changed
            # args:
            #   contact handle: int
            #   activities: dict {activity_id: str => room: int or long}
            (gobject.SIGNAL_RUN_FIRST, None, [object, object]),
        'activity-invitation':
            # We were invited to join an activity
            # args:
            #   activity ID: str
            #   activity room handle: int or long
            (gobject.SIGNAL_RUN_FIRST, None, [object, object]),
        'private-invitation':
            # We were invited to join a chat or a media call
            # args:
            #   channel object path
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'activity-properties-changed':
            # An activity's properties changed; as for
            # ActivityPropertiesChanged
            # args:
            #   activity ID: str
            #   activity room handle: int or long
            #   properties: dict { str => object }
            # FIXME: are these all the properties or just those that changed?
            (gobject.SIGNAL_RUN_FIRST, None, [object, object, object]),
    }

    def __init__(self, registry, owner):
        """Initialize the ServerPlugin instance

        registry -- telepathy.client.ManagerRegistry from the
            PresenceService, used to find the "gabble" connection
            manager in this case...
        owner -- presence.buddy.GenericOwner instance (normally a
            presence.buddy.ShellOwner instance)
        """
        gobject.GObject.__init__(self)

        self._conn = None

        self._registry = registry
        self._online_contacts = {}  # handle -> jid

        # activity id -> handle
        self._activities = {}

        self._owner = owner
        self.self_handle = None

        self._account = self._get_account_info()
        self._conn_status = CONNECTION_STATUS_DISCONNECTED
        self._reconnect_id = 0

        # Monitor IPv4 address as an indicator of the network connection
        self._ip4am = psutils.IP4AddressMonitor.get_instance()
        self._ip4am.connect('address-changed', self._ip4_address_changed_cb)

        self._publish_channel = None
        self._subscribe_channel = None
        self._subscribe_members = set()
        self._subscribe_local_pending = set()
        self._subscribe_remote_pending = set()

    @property
    def status(self):
        """Return the Telepathy connection status."""
        return self._conn_status

    def _ip4_address_changed_cb(self, ip4am, address):
        _logger.debug("::: IP4 address now %s", address)
        if address:
            _logger.debug("::: valid IP4 address, conn_status %s",
                          self._conn_status)
            if self._conn_status == CONNECTION_STATUS_DISCONNECTED:
                _logger.debug("::: will connect")
                self.start()
        else:
            _logger.debug("::: invalid IP4 address, will disconnect")
            self.cleanup()

    def _get_account_info(self):
        """Retrieve metadata dictionary describing this account

        returns dictionary with:

            server : server url from owner
            account : printable-ssh-key-hash@server
            password : ssh-key-hash
            register : whether to register (i.e. whether not yet
                registered)
        """
        account_info = {}

        account_info['server'] = self._owner.get_server()

        khash = psutils.pubkey_to_keyid(self._owner.props.key)
        account_info['account'] = "%s@%s" % (khash, account_info['server'])

        account_info['password'] = self._owner.get_key_hash()
        account_info['register'] = not self._owner.get_registered()

        print "ACCT: %s" % account_info
        return account_info

    def _find_existing_connection(self):
        """Try to find an existing Telepathy connection to this server

        filters the set of connections from
            telepathy.client.Connection.get_connections
        to find a connection using our protocol with the
        "self handle" of that connection being a handle
        which matches our account (see _get_account_info)

        returns connection or None
        """
        our_name = self._account['account']

        # Search existing connections, if any, that we might be able to use
        connections = Connection.get_connections()
        for item in connections:
            if not item.object_path.startswith(_OBJ_PATH_PREFIX):
                continue
            if item[CONN_INTERFACE].GetProtocol() != _PROTOCOL:
                continue
            if item[CONN_INTERFACE].GetStatus() == CONNECTION_STATUS_CONNECTED:
                test_handle = item[CONN_INTERFACE].RequestHandles(
                    HANDLE_TYPE_CONTACT, [our_name])[0]
                if item[CONN_INTERFACE].GetSelfHandle() != test_handle:
                    continue
            return item
        return None

    def get_connection(self):
        """Retrieve our telepathy.client.Connection object"""
        return self._conn

    def _init_connection(self):
        """Set up our connection

        if there is no existing connection
            (_find_existing_connection returns None)
        produce a new connection with our protocol for our
        account.

        if there is an existing connection, reuse it by
        registering for various of events on it.
        """
        conn = self._find_existing_connection()
        if not conn:
            acct = self._account.copy()

            # Create a new connection
            gabble_mgr = self._registry.GetManager('gabble')
            name, path = gabble_mgr[CONN_MGR_INTERFACE].RequestConnection(
                _PROTOCOL, acct)
            conn = Connection(name, path)
            del acct

        conn[CONN_INTERFACE].connect_to_signal('StatusChanged',
                                               self._status_changed_cb)
        conn[CONN_INTERFACE].connect_to_signal('NewChannel',
                                               self._new_channel_cb)

        # hack
        conn._valid_interfaces.add(CONN_INTERFACE_PRESENCE)
        conn._valid_interfaces.add(CONN_INTERFACE_BUDDY_INFO)
        conn._valid_interfaces.add(CONN_INTERFACE_ACTIVITY_PROPERTIES)
        conn._valid_interfaces.add(CONN_INTERFACE_AVATARS)
        conn._valid_interfaces.add(CONN_INTERFACE_ALIASING)

        conn[CONN_INTERFACE_PRESENCE].connect_to_signal('PresenceUpdate',
            self._presence_update_cb)

        self._conn = conn
        status = self._conn[CONN_INTERFACE].GetStatus()

        if status == CONNECTION_STATUS_DISCONNECTED:
            def connect_reply():
                _logger.debug('Connect() succeeded')
            def connect_error(e):
                _logger.debug('Connect() failed: %s', e)
                if not self._reconnect_id:
                    self._reconnect_id = gobject.timeout_add(_RECONNECT_TIMEOUT,
                            self._reconnect_cb)

            self._conn[CONN_INTERFACE].Connect(reply_handler=connect_reply,
                                               error_handler=connect_error)

        self._handle_connection_status_change(status,
                CONNECTION_STATUS_REASON_NONE_SPECIFIED)

    def _connected_cb(self):
        """Callback on successful connection to a server
        """

        if self._account['register']:
            # we successfully register this account
            self._owner.set_registered(True)

        # request both handles at the same time to reduce round-trips
        pub_handle, sub_handle = self._conn[CONN_INTERFACE].RequestHandles(
                HANDLE_TYPE_LIST, ['publish', 'subscribe'])

        # the group of contacts who may receive your presence
        publish = self._conn.request_channel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_LIST, pub_handle, True)
        self._publish_channel = publish
        publish[CHANNEL_INTERFACE_GROUP].connect_to_signal('MembersChanged',
                self._publish_members_changed_cb)
        publish_handles, local_pending, remote_pending = \
                publish[CHANNEL_INTERFACE_GROUP].GetAllMembers()

        # the group of contacts for whom you wish to receive presence
        subscribe = self._conn.request_channel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_LIST, sub_handle, True)
        self._subscribe_channel = subscribe
        subscribe[CHANNEL_INTERFACE_GROUP].connect_to_signal('MembersChanged',
                self._subscribe_members_changed_cb)
        subscribe_handles, subscribe_lp, subscribe_rp = \
                subscribe[CHANNEL_INTERFACE_GROUP].GetAllMembers()
        self._subscribe_members = set(subscribe_handles)
        self._subscribe_local_pending = set(subscribe_lp)
        self._subscribe_remote_pending = set(subscribe_rp)

        if local_pending:
            # accept pending subscriptions
            publish[CHANNEL_INTERFACE_GROUP].AddMembers(local_pending, '')

        self.self_handle = self._conn[CONN_INTERFACE].GetSelfHandle()
        self._online_contacts[self.self_handle] = self._account['account']

        # request subscriptions from people subscribed to us if we're not
        # subscribed to them
        not_subscribed = list(set(publish_handles) - set(subscribe_handles))
        subscribe[CHANNEL_INTERFACE_GROUP].AddMembers(not_subscribed, '')

        if CONN_INTERFACE_BUDDY_INFO not in self._conn.get_valid_interfaces():
            _logger.debug('OLPC information not available')
            return False

        self._conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                'PropertiesChanged', self._buddy_properties_changed_cb)
        self._conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                'ActivitiesChanged', self._buddy_activities_changed_cb)
        self._conn[CONN_INTERFACE_BUDDY_INFO].connect_to_signal(
                'CurrentActivityChanged',
                self._buddy_current_activity_changed_cb)

        self._conn[CONN_INTERFACE_AVATARS].connect_to_signal('AvatarUpdated',
                self._avatar_updated_cb)
        self._conn[CONN_INTERFACE_ALIASING].connect_to_signal('AliasesChanged',
                self._alias_changed_cb)
        self._conn[CONN_INTERFACE_ACTIVITY_PROPERTIES].connect_to_signal(
                'ActivityPropertiesChanged',
                self._activity_properties_changed_cb)

        # Request presence for everyone we're subscribed to
        self._conn[CONN_INTERFACE_PRESENCE].RequestPresence(subscribe_handles)
        return True

    def suggest_room_for_activity(self, activity_id):
        """Suggest a room to use to share the given activity.
        """
        # FIXME: figure out why the server can't figure this out itself
        return activity_id + '@conference.' + self._account['server']

    def _log_error_cb(self, msg, err):
        """Log a message (error) at debug level with prefix msg"""
        _logger.debug("Error %s: %s", msg, err)

    def _reconnect_cb(self):
        """Attempt to reconnect to the server"""
        self.start()
        return False

    def _handle_connection_status_change(self, status, reason):
        if status == self._conn_status:
            return

        if status == CONNECTION_STATUS_CONNECTING:
            self._conn_status = status
            _logger.debug("status: connecting...")
        elif status == CONNECTION_STATUS_CONNECTED:
            if self._connected_cb():
                _logger.debug("status: connected")
                self._conn_status = status
            else:
                self.cleanup()
                _logger.debug("status: was connected, but an error occurred")
        elif status == CONNECTION_STATUS_DISCONNECTED:
            self.cleanup()
            _logger.debug("status: disconnected (reason %r)", reason)
            if reason == CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED:
                # FIXME: handle connection failure; retry later?
                pass
            else:
                # If disconnected, but still have a network connection, retry
                # If disconnected and no network connection, do nothing here
                # and let the IP4AddressMonitor address-changed signal handle
                # reconnection
                if self._ip4am.props.address and not self._reconnect_id:
                    self._reconnect_id = gobject.timeout_add(_RECONNECT_TIMEOUT,
                            self._reconnect_cb)

        self.emit('status', self._conn_status, int(reason))
        return False

    def _status_changed_cb(self, status, reason):
        """Handle notification of connection-status change

        status -- CONNECTION_STATUS_*
        reason -- integer code describing the reason...
        """
        _logger.debug("::: connection status changed to %s", status)
        self._handle_connection_status_change(status, reason)

    def start(self):
        """Start up the Telepathy networking connections

        if we are already connected, query for the initial contact
        information.

        if we are already connecting, do nothing

        otherwise initiate a connection and transfer control to
            _connect_reply_cb or _connect_error_cb
        """
        _logger.debug("Starting up...")

        if self._reconnect_id > 0:
            gobject.source_remove(self._reconnect_id)
            self._reconnect_id = 0

        # Only init connection if we have a valid IP address
        if self._ip4am.props.address:
            _logger.debug("::: Have IP4 address %s, will connect",
                          self._ip4am.props.address)
            self._init_connection()
        else:
            _logger.debug("::: No IP4 address, postponing connection")

    def cleanup(self):
        """If we still have a connection, disconnect it"""
        if self._conn:
            try:
                self._conn[CONN_INTERFACE].Disconnect()
            except:
                pass
        self._conn = None
        self._conn_status = CONNECTION_STATUS_DISCONNECTED

        for handle in self._online_contacts.keys():
            self._contact_offline(handle)
        self._online_contacts = {}
        self._activities = {}

        if self._reconnect_id > 0:
            gobject.source_remove(self._reconnect_id)
            self._reconnect_id = 0

    def _contact_offline(self, handle):
        """Handle contact going offline (send message, update set)"""
        if not self._online_contacts.has_key(handle):
            return
        if self._online_contacts[handle]:
            self.emit("contact-offline", handle)
        del self._online_contacts[handle]

    def _contact_online_activities_cb(self, handle, activities):
        """Handle contact's activity list update"""
        self._buddy_activities_changed_cb(handle, activities)

    def _contact_online_activities_error_cb(self, handle, err):
        """Handle contact's activity list being unavailable"""
        _logger.debug("Handle %s - Error getting activities: %s",
                      handle, err)
        # Don't drop the buddy if we can't get their activities, for now
        #self._contact_offline(handle)

    def _contact_online_aliases_cb(self, handle, props, aliases):
        """Handle contact's alias being received (do further queries)"""
        if not self._conn or not aliases or not len(aliases):
            _logger.debug("Handle %s - No aliases", handle)
            self._contact_offline(handle)
            return

        props['nick'] = aliases[0]

        jid = self._conn[CONN_INTERFACE].InspectHandles(HANDLE_TYPE_CONTACT,
                                                        [handle])[0]
        self._online_contacts[handle] = jid
        objid = self.identify_contacts(None, [handle])[handle]

        self.emit("contact-online", objid, handle, props)

        self._conn[CONN_INTERFACE_BUDDY_INFO].GetActivities(handle,
            reply_handler=lambda *args: self._contact_online_activities_cb(
                handle, *args),
            error_handler=lambda e: self._contact_online_activities_error_cb(
                handle, e))

    def _contact_online_aliases_error_cb(self, handle, props, retry, err):
        """Handle failure to retrieve given user's alias/information"""
        if retry:
            _logger.debug("Handle %s - Error getting nickname (will retry):"
                          "%s", handle, err)
            self._conn[CONN_INTERFACE_ALIASING].RequestAliases([handle],
                reply_handler=lambda *args: self._contact_online_aliases_cb(
                    handle, props, *args),
                error_handler=lambda e: self._contact_online_aliases_error_cb(
                    handle, props, False, e))
        else:
            _logger.debug("Handle %s - Error getting nickname: %s",
                          handle, err)
            self._contact_offline(handle)

    def _contact_online_properties_cb(self, handle, props):
        """Handle failure to retrieve given user's alias/information"""
        if not props.has_key('key'):
            _logger.debug("Handle %s - invalid key.", handle)
            self._contact_offline(handle)
            return
        if not props.has_key('color'):
            _logger.debug("Handle %s - invalid color.", handle)
            self._contact_offline(handle)
            return

        self._conn[CONN_INTERFACE_ALIASING].RequestAliases([handle],
            reply_handler=lambda *args: self._contact_online_aliases_cb(
                handle, props, *args),
            error_handler=lambda e: self._contact_online_aliases_error_cb(
                handle, props, True, e))

    def _contact_online_request_properties(self, handle, tries):
        self._conn[CONN_INTERFACE_BUDDY_INFO].GetProperties(handle,
            byte_arrays=True,
            reply_handler=lambda *args: self._contact_online_properties_cb(
                handle, *args),
            error_handler=lambda e: self._contact_online_properties_error_cb(
                handle, tries, e))
        return False

    def _contact_online_properties_error_cb(self, handle, tries, err):
        """Handle error retrieving property-set for a user (handle)"""
        if tries <= 3:
            _logger.debug("Handle %s - Error getting properties (will retry):"
                          " %s", handle, err)
            tries += 1
            gobject.timeout_add(1000, self._contact_online_request_properties,
                                handle, tries)
        else:
            _logger.debug("Handle %s - Error getting properties: %s",
                          handle, err)
            self._contact_offline(handle)

    def _contact_online(self, handle):
        """Handle a contact coming online"""
        if (handle not in self._subscribe_members and
                handle not in self._subscribe_local_pending and
                handle not in self._subscribe_remote_pending):
            # it's probably a channel-specific handle - can't create a Buddy
            # object for those yet
            return

        self._online_contacts[handle] = None
        if handle == self._conn[CONN_INTERFACE].GetSelfHandle():
            jid = self._conn[CONN_INTERFACE].InspectHandles(
                    HANDLE_TYPE_CONTACT, [handle])[0]
            self._online_contacts[handle] = jid
            # ignore network events for Owner property changes since those
            # are handled locally
            return

        self._contact_online_request_properties(handle, 1)

    def _subscribe_members_changed_cb(self, message, added, removed,
                                      local_pending, remote_pending,
                                      actor, reason):

        added = set(added)
        removed = set(removed)
        local_pending = set(local_pending)
        remote_pending = set(remote_pending)

        affected = added|removed
        affected |= local_pending
        affected |= remote_pending

        self._subscribe_members -= affected
        self._subscribe_members |= added
        self._subscribe_local_pending -= affected
        self._subscribe_local_pending |= local_pending
        self._subscribe_remote_pending -= affected
        self._subscribe_remote_pending |= remote_pending

    def _publish_members_changed_cb(self, added, removed, local_pending,
            remote_pending, actor, reason):

        if local_pending:
            # accept all requested subscriptions
            self._publish_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                    local_pending, '')

        # subscribe to people who've subscribed to us, if necessary
        added = list(set(added) - self._subscribe_members
                     - self._subscribe_remote_pending)
        if added:
            self._subscribe_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                    added, '')

    def _presence_update_cb(self, presence):
        """Send update for online/offline status of presence"""
        for handle in presence:
            timestamp, statuses = presence[handle]
            online = handle in self._online_contacts
            for status, params in statuses.items():
                if not online and status == "offline":
                    # weren't online in the first place...
                    continue
                jid = self._conn[CONN_INTERFACE].InspectHandles(
                        HANDLE_TYPE_CONTACT, [handle])[0]
                olstr = "ONLINE"
                if not online: olstr = "OFFLINE"
                _logger.debug("Handle %s (%s) was %s, status now '%s'.",
                              handle, jid, olstr, status)
                if not online and status in ["available", "away", "brb",
                                             "busy", "dnd", "xa"]:
                    self._contact_online(handle)
                elif status in ["offline", "invisible"]:
                    self._contact_offline(handle)

    def _request_avatar_cb(self, handle, new_avatar_token, avatar, mime_type):
        jid = self._online_contacts[handle]
        if not jid:
            logging.debug("Handle %s not valid yet..." % handle)
            return
        icon = ''.join(map(chr, avatar))
        buddy_icon_cache.store_icon(self._conn.object_path, jid,
                                    new_avatar_token, icon)
        self.emit("avatar-updated", handle, icon)

    def _avatar_updated_cb(self, handle, new_avatar_token):
        """Handle update of given user (handle)'s avatar"""
        if handle == self._conn[CONN_INTERFACE].GetSelfHandle():
            # ignore network events for Owner property changes since those
            # are handled locally
            return

        if not self._online_contacts.has_key(handle):
            _logger.debug("Handle %s unknown.", handle)
            return

        jid = self._online_contacts[handle]
        if not jid:
            _logger.debug("Handle %s not valid yet...", handle)
            return

        icon = buddy_icon_cache.get_icon(self._conn.object_path, jid,
                                         new_avatar_token)
        if not icon:
            # cache miss
            self._conn[CONN_INTERFACE_AVATARS].RequestAvatar(handle,
                    reply_handler=lambda *args: self._request_avatar_cb(handle,
                        new_avatar_token, *args),
                    error_handler=lambda e: self._log_error_cb(
                        "getting avatar", e))
        else:
            self.emit("avatar-updated", handle, icon)

    def _alias_changed_cb(self, aliases):
        """Handle update of aliases for all users"""
        for handle, alias in aliases:
            prop = {'nick': alias}
            #print "Buddy %s alias changed to %s" % (handle, alias)
            if (self._online_contacts.has_key(handle) and
                    self._online_contacts[handle]):
                self._buddy_properties_changed_cb(handle, prop)

    def _buddy_properties_changed_cb(self, handle, properties):
        """Handle update of given user (handle)'s properties"""
        if handle == self._conn[CONN_INTERFACE].GetSelfHandle():
            # ignore network events for Owner property changes since those
            # are handled locally
            return
        if (self._online_contacts.has_key(handle) and
                self._online_contacts[handle]):
            self.emit("buddy-properties-changed", handle, properties)

    def _buddy_activities_changed_cb(self, handle, activities):
        """Handle update of given user (handle)'s activities"""
        if handle == self._conn[CONN_INTERFACE].GetSelfHandle():
            # ignore network events for Owner activity changes since those
            # are handled locally
            return
        if (not self._online_contacts.has_key(handle) or
                not self._online_contacts[handle]):
            return

        activities_dict = {}
        for act_id, act_handle in activities:
            self._activities[act_id] = act_handle
            activities_dict[act_id] = act_handle
        self.emit("buddy-activities-changed", handle, activities_dict)

    def _buddy_current_activity_changed_cb(self, handle, activity, channel):
        """Handle update of given user (handle)'s current activity"""

        if handle == self._conn[CONN_INTERFACE].GetSelfHandle():
            # ignore network events for Owner current activity changes since
            # those are handled locally
            return
        if (not self._online_contacts.has_key(handle) or
                not self._online_contacts[handle]):
            return

        if not len(activity) or not util.validate_activity_id(activity):
            activity = None
        prop = {'current-activity': activity}
        _logger.debug("Handle %s: current activity now %s", handle, activity)
        self._buddy_properties_changed_cb(handle, prop)

    def _new_channel_cb(self, object_path, channel_type, handle_type, handle,
                        suppress_handler):
        """Handle creation of a new channel
        """
        if (handle_type == HANDLE_TYPE_ROOM and
            channel_type == CHANNEL_TYPE_TEXT):
            def ready(channel):

                for act_id, act_handle in self._activities.iteritems():
                    if handle == act_handle:
                        break
                    else:
                        return

                def got_all_members(current, local_pending, remote_pending):
                    if local_pending:
                        for act_id, act_handle in self._activities.iteritems():
                            if handle == act_handle:
                                self.emit('activity-invitation', act_id, handle)
                def got_all_members_err(e):
                    logger.debug('Unable to get channel members for %s:',
                                 object_path, exc_info=1)

                group = channel[CHANNEL_INTERFACE_GROUP]
                group.GetAllMembers(reply_handler=got_all_members,
                                    error_handler=got_all_members_err)

            # we throw away the channel as soon as ready() finishes
            Channel(self._conn.service_name, object_path,
                    ready_handler=ready)

        elif (handle_type == HANDLE_TYPE_CONTACT and
              channel_type in (CHANNEL_TYPE_TEXT,
                               CHANNEL_TYPE_STREAMED_MEDIA)):
            self.emit("private-invitation", object_path)

    def _activity_properties_changed_cb(self, room, properties):
        """Handle update of properties for a "room" (activity handle)"""
        for act_id, act_handle in self._activities.items():
            if room == act_handle:
                self.emit("activity-properties-changed", act_id, room, properties)
                return

    def _server_is_trusted(self, hostname):
        """Return True if the server with the given hostname is trusted to
        verify public-key ownership correctly, and only allows users to
        register JIDs whose username part is either a public key fingerprint,
        or of the wrong form to be a public key fingerprint (to allow for
        ejabberd's admin@example.com address).

        If we trust the server, we can skip verifying the key ourselves,
        which leads to simplifications. In the current implementation we
        never verify that people actually own the key they claim to, so
        we will always give contacts on untrusted servers a JID- rather than
        key-based identity.

        For the moment we assume that the test server, olpc.collabora.co.uk,
        does this verification.
        """
        return (hostname == 'olpc.collabora.co.uk')

    def identify_contacts(self, tp_chan, handles):
        """Work out the "best" unique identifier we can for the given handles,
        in the context of the given channel (which may be None), using only
        'fast' connection manager API (that does not involve network
        round-trips).

        For the XMPP server case, we proceed as follows:

        * Find the owners of the given handles, if the channel has
          channel-specific handles
        * If the owner (globally-valid JID) is on a trusted server, return
          'keyid/' plus the 'key fingerprint' (the user part of their JID,
          currently implemented as the SHA-1 of the Base64 blob in
          owner.key.pub)
        * If the owner (globally-valid JID) cannot be found or is on an
          untrusted server, return 'xmpp/' plus an escaped form of the JID

        The idea is that we identify buddies by key-ID (i.e. by key, assuming
        no collisions) if we can find it without making network round-trips,
        but if that's not possible we just use their JIDs.

        :Parameters:
            `tp_chan` : telepathy.client.Channel or None
                The channel in which the handles were found, or None if they
                are known to be channel-specific handles
            `handles` : iterable over (int or long)
                The contacts' handles in that channel
        :Returns:
            A dict mapping the provided handles to the best available
            unique identifier, which is a string that could be used as a
            suffix to an object path
        """
        # we need to be able to index into handles, so force them to
        # be a sequence
        if not isinstance(handles, (tuple, list)):
            handles = tuple(handles)

        owners = handles

        if tp_chan is not None and CHANNEL_INTERFACE_GROUP in tp_chan:
            group = tp_chan[CHANNEL_INTERFACE_GROUP]
            if (group.GetGroupFlags() &
                CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES):
                owners = group.GetHandleOwners(handles)
                for i, owner in enumerate(owners):
                    if owner == 0:
                        owners[i] = handles[i]
        else:
            group = None

        jids = self._conn[CONN_INTERFACE].InspectHandles(HANDLE_TYPE_CONTACT,
                                                         owners)

        ret = {}
        for handle, jid in zip(handles, jids):
            # special-case the Owner - we always know who we are
            if (handle == self.self_handle or
                (group is not None and handle == group.GetSelfHandle())):
                ret[handle] = self._owner.props.objid
                continue

            if '/' in jid:
                # the contact is unidentifiable (in an anonymous MUC) - create
                # a temporary identity for them, based on their room-JID
                ret[handle] = 'xmpp/' + psutils.escape_identifier(jid)
            else:
                user, host = jid.split('@', 1)
                if (self._server_is_trusted(host) and len(user) == 40 and
                    user.strip(hexdigits) == ''):
                    # they're on a trusted server and their username looks
                    # like a key-ID
                    ret[handle] = 'keyid/' + user.lower()
                else:
                    # untrusted server, or not the right format to be a
                    # key-ID - identify the contact by their JID
                    ret[handle] = 'xmpp/' + psutils.escape_identifier(jid)

        return ret
