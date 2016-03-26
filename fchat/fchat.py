#!/usr/bin/python
# -*- coding: utf-8 -*-

__all__ = ['UserDoesNotExist', 'User', 'Channel', 'FChatClient', 'auto_reconnect']

from ws4py.client.threadedclient import WebSocketClient
from ws4py.exc import WebSocketException
import urllib
import urllib.request
import json
import time
import logging
import threading


def auto_reconnect(func):
    def inner(self):
        while True:
            self.logger.info("Connecting ...")
            try:
                self.setup()
                self.connect()
                func(self)
            except KeyboardInterrupt:
                self.close()
                return
            except WebSocketException as e:
                self.logger.exception("WebSocketException")
            except ConnectionError as e:
                self.logger.error("Could not connect to server!")
            except Exception:
                self.logger.exception("Exception")
            finally:
                self.terminate_outgoing_thread()
                self.logger.info("Trying to reconnect in %d seconds (%d attempt) ..." % (self.reconnect_delay, self.reconnect_attempt))
                time.sleep(self.reconnect_delay)

                if self.reconnect_delay < 120:
                    self.reconnect_delay *= 2

                self.reconnect_attempt += 1
                WebSocketClient.__init__(self, self.url)

    return inner


class UserDoesNotExist(Exception):
    pass


class User(object):
    def __init__(self, name, gender, status, message):
        self.name = name
        self.gender = gender
        self.status = status
        self.message = message

    def update(self, status, message):
        self.status = status
        self.message = message


class Channel(object):
    def __init__(self, name, title):
        self.name = name
        self.title = title
        self.description = ""
        self.users = []
        self.operator_names = []
        self.founder_name = ""

    def add_user(self, user):
        if user not in self.users:
            self.users.append(user)

    def remove_user(self, user):
        if user in self.users:
            self.users.remove(user)

    def user_exists(self, user):
        return user in self.users

    def user_is_operator(self, user):
        for name in self.operator_names:
            if user.name.lower() == name.lower():
                return True
        return False

    def user_is_founder(self, user):
        return user.name.lower() == self.founder_name.lower()

    def set_description(self, desc):
        self.description = desc

    def set_operator_names(self, op_names):
        self.op_names = op_names

    def set_founder_name(self, founder_name):
        self.founder_name = founder_name


class OutgoingPumpThread(threading.Thread):
    """F-Chat has a minimum send delay between messages.
    This thread makes sure we are never violating this.
    """
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.delay = 1
        self.running = True

    def run(self):
        while self.running:
            if len(self.client.outgoing_buffer):
                self.client.send_one()
            time.sleep(self.delay)

    def set_delay(self, delay):
        self.delay = delay


class FChatClient(WebSocketClient):
    logger = logging.getLogger("fchat")

    def __init__(self, url, account, password, character, client_name="Python FChat Library"):
        WebSocketClient.__init__(self, url)
        self.account = account
        self.password = password
        self.character = character
        self.client_name = client_name
        self.reconnect_delay = 1
        self.reconnect_attempt = 0

    def setup(self):
        self.server_vars = {}
        self.operators = []
        self.friends = []
        self.ignored_users = []
        self.users = {}
        self.channels = {}
        self.own_user = None
        self.outgoing_buffer = []
        self.last_ping_time = time.time()

        self.ticket = self.get_ticket()

        if self.ticket is None:
            return False

        self.buffer_lock = threading.Lock()
        self.outgoing_thread = OutgoingPumpThread(self)

        self.outgoing_thread.start()

        return True

    def get_ticket(self):
        self.logger.info("Fetching ticket ...")

        data = {'account': self.account, 'password': self.password}
        data_enc = urllib.parse.urlencode(data)
        data_enc = data_enc.encode("UTF-8")

        response = urllib.request.urlopen('http://www.f-list.net/json/getApiTicket.php', data_enc)
        text = response.read()
        text_parsed = json.loads(text.decode("UTF-8"))

        if 'ticket' in text_parsed:
            return text_parsed['ticket']
        else:
            self.logger.error(text_parsed['error'])
            return None

    def terminate_outgoing_thread(self):
        if self.outgoing_thread.running:
            self.outgoing_thread.running = False
            self.outgoing_thread.join()

    def opened(self):
        self.reconnect_delay = 1
        self.reconnect_attempt = 0
        self.logger.info("Connected!")
        self.IDN(self.character)

    def closed(self, code, reason=None):
        self.logger.info("Closing (" + str(code) + ", " + str(reason) + ")!")
        self.terminate_outgoing_thread()

        super().closed(code, reason)

    def received_message(self, m):
        msg = m.data.decode("UTF-8")
        command = msg[:3]
        command_handler = "on_" + command

        try:
            json_string = msg[4:]
            data = json.loads(json_string)
        except:
            json_string = "{}"
            data = {}

        self.logger.debug("<< %s %s" % (command.encode('UTF-8'), json_string.encode('UTF-8')))

        if hasattr(self, command_handler):
            try:
                getattr(self, command_handler)(data)
            except Exception:
                self.logger.exception("Error running handler for " + command + "!")
        else:
            self.logger.warning("Unhandled event: " + command)

    def send_message(self, cmd, data):
        self.buffer_lock.acquire()
        self.outgoing_buffer.append((cmd, json.dumps(data)))
        self.buffer_lock.release()

    def send_one(self):
        self.buffer_lock.acquire()
        cmd, data = self.outgoing_buffer.pop(0)
        self.logger.debug(">> %s %s" % (cmd, data))
        self.send(cmd + " " + data)
        self.buffer_lock.release()

    def add_user(self, user):
        self.users[user.name.lower()] = user

    def remove_user(self, user):
        for _, channel in self.channels.items():
            channel.remove_user(user)

        del self.users[user.name.lower()]

    def remove_user_by_name(self, user_name):
        user = self.get_user_by_name(user_name)

        if user:
            self.remove_user(user)

    def user_exists_by_name(self, user_name):
        return user_name.lower() in self.users

    def user_common_channels(self, user):
        channels = []
        for _, channel in self.channels.items():
            if channel.user_exists(user):
                channels.append(channel)

        return channels

    def get_user_by_name(self, name):
        name = name.lower()

        try:
            return self.users[name]
        except KeyError:
            return None

    def add_channel(self, channel):
        self.channels[channel.name.lower()] = channel

    def remove_channel(self, channel):
        del self.channels[channel.name.lower()]

    def remove_channel_by_name(self, channel_name):
        channel = self.get_channel_by_name(channel_name)
        if channel:
            self.remove_channel(channel)

    def channel_exists_by_name(self, channel_name):
        return channel_name.lower() in self.channels

    def get_channel_by_name(self, channel_name):
        channel_name = channel_name.lower()
        try:
            return self.channels[channel_name]
        except KeyError:
            return None

    # - Event Handlers
    # Ping
    def on_PIN(self, data):
        self.last_ping_time = time.time()
        self.send_message("PIN", {})

    # Identification successful
    def on_IDN(self, data):
        self.on_identified(data['character'])

    # Channel invite
    def on_CIU(self, data):
        sender = self.get_user_by_name(data['sender'])

        if sender is None:
            return

        channel_name = ""
        if "name" in data:
            channel_name = data['name']
        elif "channel" in data:
            channel_name = data['channel']

        title = ''
        if "title" in data:
            title = data['title']

        self.on_invite(sender, channel_name, title)

    # Server variables
    def on_VAR(self, data):
        self.server_vars[data['variable']] = data['value']

        # fine tune outgoing message pump
        if data['variable'] == 'msg_flood':
            delay = float(data['value']) * 1.5
            self.logger.debug("Fine tuned outgoing message delay to %f." % (delay))
            # Increase the value by 50%, just to make sure
            self.outgoing_thread.set_delay(delay)

    # List of chanops
    def on_ADL(self, data):
        self.operators = data['ops']

    # Friend list
    def on_FRL(self, data):
        self.friends = data['characters']

    # Ignore list management
    def on_IGN(self, data):
        self.ignored_users = data['characters']

    # Global user list
    def on_LIS(self, data):
        for user in data['characters']:
            self.add_user(User(user[0], user[1], user[2], user[3]))

            if user[0] == self.character:
                self.own_user = self.get_user_by_name(user[0])

    # Server hello command
    def on_HLO(self, data):
        self.logger.debug("Server message: '%s'" % (data['message']))

    # Number of connected users
    def on_CON(self, data):
        self.logger.debug("Connected users: %s" % (data['count']))

    # New user connected
    def on_NLN(self, data):
        if not self.user_exists_by_name(data['identity']):
            self.add_user(User(data['identity'], data['gender'], data['status'], ''))

        user = self.get_user_by_name(data['identity'])
        self.on_user_connected(user)

    # User disconnected
    def on_FLN(self, data):
        user = self.get_user_by_name(data['character'])
        self.on_user_disconnected(user)
        self.remove_user(user)

    # User status change
    def on_STA(self, data):
        user = self.get_user_by_name(data['character'])
        if user:
            user.update(data['status'], data['statusmsg'])
            self.on_user_status_changed(user)

    # User joined channel
    def on_JCH(self, data):
        channel_name = data['channel']

        if not self.channel_exists_by_name(channel_name):
            self.add_channel(Channel(channel_name, data['title']))

        channel = self.get_channel_by_name(channel_name)
        user = self.get_user_by_name(data['character']['identity'])
        channel.add_user(user)

        self.on_join(channel, user)

    # Initial channel data
    def on_ICH(self, data):
        channel = self.get_channel_by_name(data['channel'])
        for user_data in data['users']:
            user = self.get_user_by_name(user_data['identity'])
            channel.add_user(user)

        self.on_channel_data(channel)

    # User leaves channel
    def on_LCH(self, data):
        channel = self.get_channel_by_name(data['channel'])
        user = self.get_user_by_name(data['character'])
        channel.remove_user(user)

        self.on_leave(channel, user)

        if user == self.own_user:
            self.remove_channel(channel)

    # Channel Operator List
    def on_COL(self, data):
        channel = self.get_channel_by_name(data['channel'])
        opers = []

        founder = data['oplist'].pop(0)

        for user_name in data['oplist']:
            opers.append(user_name)

        channel.set_founder_name(founder)
        channel.set_operator_names(opers)

        self.on_channel_operator_list(channel, founder, opers)

    # Change channel description
    def on_CDS(self, data):
        channel = self.get_channel_by_name(data['channel'])
        channel.set_description(data['description'])
        self.on_channel_description(channel, data['description'])

    # Private message
    def on_PRI(self, data):
        msg = data['message']
        user = self.get_user_by_name(data['character'])

        if user is None:
            self.logger.error("Received message from user who is not on my user list (Name: '%s')" % (data['character']))
            return

        self.on_message(user, msg)

    # Channel message
    def on_MSG(self, data):
        msg = data['message']
        user = self.get_user_by_name(data['character'])

        if user is None:
            self.logger.error("Received message from user who is not on my user list (Name: '%s')" % (data['character']))
            return

        channel = self.get_channel_by_name(data['channel'])

        if channel is None:
            self.logger.error("Received message in channel I'm not even in (Channel: '%s')!" % (data['channel']))
            return

        self.on_message(user, msg, channel)

    def on_ERR(self, data):
        self.on_error(data['number'], data['message'])

    # User is typing
    def on_TPN(self, data):
        user = self.get_user_by_name(data['character'])
        status = data['status']

        self.on_user_typing(user, status)

    # Combined message handler
    def on_message(self, user, msg, channel=None):
        pass

    def on_identified(self, character):
        pass

    def on_invite(self, sender, channel_name, title):
        pass

    def on_join(self, channel, user):
        pass

    def on_leave(self, channel, user):
        pass

    def on_channel_data(self, channel):
        pass

    def on_channel_description(self, channel, description):
        pass

    def on_channel_operator_list(self, channel, founder_name, operator_names):
        pass

    def on_error(self, number, message):
        pass

    def on_user_status_changed(self, user):
        pass

    def on_user_connected(self, user):
        pass

    def on_user_disconnected(self, user):
        pass

    def on_user_typing(self, user, status):
        pass

    # - FChat Commands
    def IDN(self, character):
        data = {'account': self.account,
                'character': character,
                'ticket': self.ticket,
                'cname': self.client_name,
                'cversion': '0.0.2',
                'method': 'ticket'}
        self.send_message("IDN", data)

    def JCH(self, channel):
        self.send_message("JCH", {'channel': channel})

    def LCH(self, channel):
        self.send_message("LCH", {'channel': channel})

    def MSG(self, channel, message):
        data = {'channel': channel, 'message': message}
        self.send_message("MSG", data)

    def PRI(self, recipient, message):
        data = {'recipient': recipient, 'message': message}
        self.send_message("PRI", data)

    # - Higher level commands
    def private_message(self, user, message):
        self.PRI(user.name, message)

    def channel_message(self, channel, message):
        self.MSG(channel.name, message)


if __name__ == "__main__":
    import sys
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.DEBUG)
    logging.info("Starting up FChatClient!")
    try:
        ws = FChatClient('ws://chat.f-list.net:8722', sys.argv[1], sys.argv[2], sys.argv[3])
        ws.setup()
        ws.connect()
        ws.run_forever()
    except KeyboardInterrupt:
        ws.close()
