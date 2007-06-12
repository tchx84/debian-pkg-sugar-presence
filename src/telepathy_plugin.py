"""Base class for Telepathy plugins."""

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

import logging
from itertools import izip

import gobject

from telepathy.client import Channel
from telepathy.constants import (CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_CONNECTING, CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_REASON_AUTHENTICATION_FAILED,
    CONNECTION_STATUS_REASON_NONE_SPECIFIED,
    HANDLE_TYPE_CONTACT, HANDLE_TYPE_ROOM, HANDLE_TYPE_LIST)
from telepathy.interfaces import (CONN_INTERFACE, CHANNEL_TYPE_TEXT,
        CHANNEL_TYPE_STREAMED_MEDIA, CHANNEL_INTERFACE_GROUP,
        CONN_INTERFACE_PRESENCE, CONN_INTERFACE_AVATARS,
        CONN_INTERFACE_ALIASING, CHANNEL_TYPE_CONTACT_LIST)


CONN_INTERFACE_BUDDY_INFO = 'org.laptop.Telepathy.BuddyInfo'
CONN_INTERFACE_ACTIVITY_PROPERTIES = 'org.laptop.Telepathy.ActivityProperties'


_logger = logging.getLogger('s-p-s.server_plugin')


class TelepathyPlugin(gobject.GObject):
    __gsignals__ = {
        'contacts-online':
            # Contacts in the subscribe list have come online.
            # args:
            #   contact identification (based on key ID or JID): list of str
            #   contact handle: list of int or long
            #   contact identifier (JID): list of str or unicode
            (gobject.SIGNAL_RUN_FIRST, None, [object, object, object]),
        'contacts-offline':
            # Contacts in the subscribe list have gone offline.
            # args: iterable over contact handles
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'status':
            # Connection status changed.
            # args: status, reason as for Telepathy StatusChanged
            (gobject.SIGNAL_RUN_FIRST, None, [int, int]),
        'activity-invitation':
            # We were invited to join an activity
            # args:
            #   activity room handle: int or long
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
        'private-invitation':
            # We were invited to join a chat or a media call
            # args:
            #   channel object path
            (gobject.SIGNAL_RUN_FIRST, None, [object]),
    }

    _RECONNECT_TIMEOUT = 5000

    def __init__(self, registry, owner):
        """Initialize the ServerPlugin instance

        :Parameters:
            `registry` : telepathy.client.ManagerRegistry
                From the PresenceService, used to find the connection
                manager details
            `owner` : buddy.GenericOwner
                The Buddy representing the owner of this XO (normally a
                buddy.ShellOwner instance)
        """
        gobject.GObject.__init__(self)

        #: The connection, a `telepathy.client.Connection`
        self._conn = None

        #: List of dbus-python SignalMatch objects representing signal match
        #: rules associated with the connection, so we don't leak the match
        #: rules when disconnected.
        self._matches = []

        #: The manager registry, a `telepathy.client.ManagerRegistry`
        self._registry = registry

        #: set of contact handles: those for whom we've emitted contacts-online
        self._online_contacts = set()

        #: The owner, a `buddy.GenericOwner`
        self._owner = owner
        #: The owner's handle on this connection
        self.self_handle = None
        #: The owner's identifier (e.g. JID) on this connection
        self.self_identifier = None

        #: The connection's status
        self._conn_status = CONNECTION_STATUS_DISCONNECTED

        #: GLib signal ID for reconnections
        self._reconnect_id = 0

        #: Parameters for the connection manager
        self._account = self._get_account_info()

        #: The ``subscribe`` channel: a `telepathy.client.Channel` or None
        self._subscribe_channel = None
        #: The members of the ``subscribe`` channel
        self._subscribe_members = set()
        #: The local-pending members of the ``subscribe`` channel
        self._subscribe_local_pending = set()
        #: The remote-pending members of the ``subscribe`` channel
        self._subscribe_remote_pending = set()

        #: The ``publish`` channel: a `telepathy.client.Channel` or None
        self._publish_channel = None

    @property
    def status(self):
        """Return the Telepathy connection status."""
        return self._conn_status

    def get_connection(self):
        """Retrieve our telepathy.client.Connection object"""
        return self._conn

    def _get_account_info(self):
        """Retrieve connection manager parameters for this account
        """
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def suggest_room_for_activity(self, activity_id):
        """Suggest a room to use to share the given activity.
        """
        return activity_id

    def identify_contacts(self, tp_chan, handles, identifiers=None):
        raise NotImplementedError

    def _reconnect_cb(self):
        """Attempt to reconnect to the server"""
        self.start()
        return False

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
            conn = self._make_new_connection()

        m = conn[CONN_INTERFACE].connect_to_signal('StatusChanged',
           self._handle_connection_status_change)
        self._matches.append(m)
        m = conn[CONN_INTERFACE].connect_to_signal('NewChannel',
                                                   self._new_channel_cb)
        self._matches.append(m)

        # hack
        conn._valid_interfaces.add(CONN_INTERFACE_PRESENCE)
        conn._valid_interfaces.add(CONN_INTERFACE_BUDDY_INFO)
        conn._valid_interfaces.add(CONN_INTERFACE_ACTIVITY_PROPERTIES)
        conn._valid_interfaces.add(CONN_INTERFACE_AVATARS)
        conn._valid_interfaces.add(CONN_INTERFACE_ALIASING)

        m = conn[CONN_INTERFACE_PRESENCE].connect_to_signal('PresenceUpdate',
            self._presence_update_cb)
        self._matches.append(m)

        self._conn = conn
        status = self._conn[CONN_INTERFACE].GetStatus()

        if status == CONNECTION_STATUS_DISCONNECTED:
            def connect_reply():
                _logger.debug('Connect() succeeded')
            def connect_error(e):
                _logger.debug('Connect() failed: %s', e)
                if not self._reconnect_id:
                    self._reconnect_id = gobject.timeout_add(self._RECONNECT_TIMEOUT,
                            self._reconnect_cb)

            self._conn[CONN_INTERFACE].Connect(reply_handler=connect_reply,
                                               error_handler=connect_error)

        self._handle_connection_status_change(status,
                CONNECTION_STATUS_REASON_NONE_SPECIFIED)

    def _find_existing_connection(self):
        raise NotImplementedError

    def _make_new_connection(self):
        raise NotImplementedError

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
                if self._could_connect() and not self._reconnect_id:
                    self._reconnect_id = gobject.timeout_add(self._RECONNECT_TIMEOUT,
                            self._reconnect_cb)

        self.emit('status', self._conn_status, int(reason))

    def _connected_cb(self):
        raise NotImplementedError

    def _could_connect(self):
        return True

    def cleanup(self):
        """If we still have a connection, disconnect it"""

        matches = self._matches
        self._matches = []
        for match in matches:
            match.remove()

        if self._conn:
            try:
                self._conn[CONN_INTERFACE].Disconnect()
            except:
                pass
        self._conn = None
        self._conn_status = CONNECTION_STATUS_DISCONNECTED

        if self._online_contacts:
            self._contacts_offline(self._online_contacts)

        if self._reconnect_id > 0:
            gobject.source_remove(self._reconnect_id)
            self._reconnect_id = 0

    def _contacts_offline(self, handles):
        """Handle contacts going offline (send message, update set)"""
        self._online_contacts -= handles
        _logger.debug('Contacts now offline: %r', handles)
        self.emit("contacts-offline", handles)

    def _contacts_online(self, handles):
        """Handle contacts coming online"""
        relevant = []

        for handle in handles:
            if handle == self.self_handle:
                # ignore network events for Owner property changes since those
                # are handled locally
                pass
            elif (handle in self._subscribe_members or
                  handle in self._subscribe_local_pending or
                  handle in self._subscribe_remote_pending):
                relevant.append(handle)
            # else it's probably a channel-specific handle - can't create a
            # Buddy object for those yet

        if not relevant:
            return

        jids = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, relevant)

        handle_to_objid = self.identify_contacts(None, relevant, jids)
        objids = []
        for handle in relevant:
            objids.append(handle_to_objid[handle])

        self._online_contacts |= frozenset(relevant)
        _logger.debug('Contacts now online:')
        for handle, objid in izip(relevant, objids):
            _logger.debug('  %u .../%s', handle, objid)
        self.emit('contacts-online', objids, relevant, jids)

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
        if self._subscribe_channel is not None:
            added = list(set(added) - self._subscribe_members
                         - self._subscribe_remote_pending)
            if added:
                self._subscribe_channel[CHANNEL_INTERFACE_GROUP].AddMembers(
                        added, '')

    def _presence_update_cb(self, presence):
        """Send update for online/offline status of presence"""

        now_online = set()
        now_offline = set(presence.iterkeys())

        for handle in presence:
            timestamp, statuses = presence[handle]
            for status, params in statuses.items():
                # FIXME: use correct logic involving the GetStatuses method
                if status in ["available", "away", "brb", "busy", "dnd", "xa"]:
                    now_online.add(handle)
                    now_offline.discard(handle)

        now_online -= self._online_contacts
        now_offline &= self._online_contacts

        if now_online:
            self._contacts_online(now_online)
        if now_offline:
            self._contacts_offline(now_offline)

    def _new_channel_cb(self, object_path, channel_type, handle_type, handle,
                        suppress_handler):
        """Handle creation of a new channel
        """
        if (handle_type == HANDLE_TYPE_ROOM and
            channel_type == CHANNEL_TYPE_TEXT):
            def ready(channel):
                def got_all_members(current, local_pending, remote_pending):
                    if local_pending:
                        self.emit('activity-invitation', handle)
                def got_all_members_err(e):
                    _logger.debug('Unable to get channel members for %s:',
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
        m = publish[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._publish_members_changed_cb)
        self._matches.append(m)
        publish_handles, local_pending, remote_pending = \
                publish[CHANNEL_INTERFACE_GROUP].GetAllMembers()

        # the group of contacts for whom you wish to receive presence
        subscribe = self._conn.request_channel(CHANNEL_TYPE_CONTACT_LIST,
                HANDLE_TYPE_LIST, sub_handle, True)
        self._subscribe_channel = subscribe
        m = subscribe[CHANNEL_INTERFACE_GROUP].connect_to_signal(
                'MembersChanged', self._subscribe_members_changed_cb)
        self._matches.append(m)
        subscribe_handles, subscribe_lp, subscribe_rp = \
                subscribe[CHANNEL_INTERFACE_GROUP].GetAllMembers()
        self._subscribe_members = set(subscribe_handles)
        self._subscribe_local_pending = set(subscribe_lp)
        self._subscribe_remote_pending = set(subscribe_rp)

        if local_pending:
            # accept pending subscriptions
            # FIXME: do this async
            publish[CHANNEL_INTERFACE_GROUP].AddMembers(local_pending, '')

        # FIXME: do this async?
        self.self_handle = self._conn[CONN_INTERFACE].GetSelfHandle()
        self.self_identifier = self._conn[CONN_INTERFACE].InspectHandles(
                HANDLE_TYPE_CONTACT, [self.self_handle])[0]

        # request subscriptions from people subscribed to us if we're not
        # subscribed to them
        not_subscribed = list(set(publish_handles) - set(subscribe_handles))
        subscribe[CHANNEL_INTERFACE_GROUP].AddMembers(not_subscribed, '')

        # Request presence for everyone we're subscribed to
        self._conn[CONN_INTERFACE_PRESENCE].RequestPresence(subscribe_handles)
        return True

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
        if self._could_connect():
            self._init_connection()
        else:
            _logger.debug('Postponing connection')
