#!/usr/bin/python
# -*- coding: utf-8 -*-

from ws4py.client.threadedclient import WebSocketClient
import urllib
import urllib.request
import json
import time
import logging
import threading

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

    def add_user(self, user):
        self.users.append(user)

    def remove_user(self, user):
        if user in self.users:
            self.users.remove(user)

    def set_description(self, desc):
        self.description = desc


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
    def __init__(self, url, account, password, character):
        WebSocketClient.__init__(self, url)
        self.account = account
        self.password = password
        self.character = character
        self.server_vars = {}
        self.operators = []
        self.friends = []
        self.ignored_users = []
        self.own_user = None
        self.users = {}
        self.channels = {}

        self.outgoing_buffer = []
        self.buffer_lock = threading.Lock()
        self.outgoing_thread = OutgoingPumpThread(self)

        self.ticket = self.get_ticket()

        if self.ticket == None:
            return

        self.outgoing_thread.start()

    def get_ticket(self):
        logging.info("Fetching ticket ...")

        data = {'account': self.account, 'password': self.password}
        data_enc = urllib.parse.urlencode(data)
        data_enc = data_enc.encode("UTF-8")

        response = urllib.request.urlopen('http://www.f-list.net/json/getApiTicket.php', data_enc)
        text = response.read()
        text_parsed = json.loads(text.decode("UTF-8"))

        if 'ticket' in text_parsed:
            return text_parsed['ticket']
        else:
            logging.error(text_parsed['error'])
            return None

    def opened(self):
        self.IDN(self.character)

    def closed(self, code, reason=None):
        logging.info("Closing (" + str(code) + ", " + str(reason) + ")!")

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

        logging.debug("<< " + command + " " + json_string)

        if hasattr(self, command_handler):
            try:
                getattr(self, command_handler)(data)
            except Exception as e:
                logging.exception("Error running handler for " + command + "!")
        else:
            logging.warning("Unhandled event: " + command)

    def send_message(self, cmd, data):
        self.buffer_lock.acquire()
        self.outgoing_buffer.append((cmd, json.dumps(data)))
        self.buffer_lock.release()

    def send_one(self):
        self.buffer_lock.acquire()
        cmd, data = self.outgoing_buffer.pop(0)
        logging.debug(">> " + cmd + " " + data)
        self.send(cmd + " " + data)
        self.buffer_lock.release()

    def get_user_by_character(self, character):
        if character in self.users:
            return self.users[character]
        else:
            return None

    def get_channel(self, channel):
        if channel in self.channels:
            return self.channels[channel]
        else:
            return None

    def close(self):
        self.outgoing_thread.running = False
        self.outgoing_thread.join()
        super().close()

    # - Event Handlers
    # Ping
    def on_PIN(self, data):
        self.send_message("PIN", {})

    # Identification successful
    def on_IDN(self, data):
        pass

    # Channel invite
    def on_CIU(self, data):
        pass

    # Server variables
    def on_VAR(self, data):
        self.server_vars[data['variable']] = data['value']

        # fine tune outgoing message pump
        if data['variable'] == 'msg_flood':
            delay = float(data['value']) * 1.2
            logging.debug("Fine tuned outgoing message delay to %f." % (delay))
            # Increase the value by 20%, just to make sure
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
            self.users[user[0]] = User(user[0], user[1], user[2], user[3])

            if user[0] == self.character:
                self.own_user = self.users[user[0]]

    # Server hello command
    def on_HLO(self, data):
        logging.debug("Server message: '%s'" % (data['message']))

    # Number of connected users
    def on_CON(self, data):
        logging.debug("Connected users: %s" % (data['count']))

    # New user connected
    def on_NLN(self, data):
        if data['identity'] not in self.users:
            self.users[data['identity']] = User(data['identity'], data['gender'], data['status'], '')

    # User disconnected
    def on_FLN(self, data):
        if data['character'] in self.users:
            for channel in self.channels.values():
                channel.remove_user(self.users[data['character']])

            del self.users[data['character']]

    # User status change
    def on_STA(self, data):
        if data['character'] in self.users:
            self.users[data['character']].update(data['status'], data['statusmsg'])

    # User joined channel
    def on_JCH(self, data):
        if data['channel'] not in self.channels:
            self.channels[data['channel']] = Channel(data['channel'], data['title'])

        self.channels[data['channel']].add_user(self.users[data['character']['identity']])

    # Initial channel data
    def on_ICH(self, data):
        for user in data['users']:
            self.channels[data['channel']].add_user(self.users[user['identity']])

    # User leaves channel
    def on_LCH(self, data):
        self.channels[data['channel']].remove_user(self.users[data['character']])

        if data['character'] == self.character:
            del self.channels[data['channel']]

    # Change channel description
    def on_CDS(self, data):
        self.channels[data['channel']].set_description(data['description'])

    # Private message
    def on_PRI(self, data):
        msg = data['message']
        user = self.get_user_by_character(data['character'])

        if user is None:
            logging.error("Received message from user who is not on my user list (Name: '%s')" % (data['character']))
            return

        self.on_message(user, msg)

    # Channel message
    def on_MSG(self, data):
        msg = data['message']
        user = self.get_user_by_character(data['character'])

        if user is None:
            logging.error("Received message from user who is not on my user list (Name: '%s')" % (data['character']))
            return

        channel = self.get_channel(data['channel'])

        if channel is None:
            logging.error("Received message in channel I'm not even in (Channel: '%s')!" % (data['channel']))
            return

        self.on_message(user, msg, channel)

    # Combined message handler
    def on_message(self, user, msg, channel=None):
        pass

    # - FChat Commands
    def IDN(self, character):
        data = {'account': self.account, 'character': character, 'ticket': self.ticket,
                        'cname': 'Python FChat Library', 'cversion': '0.0.2', 'method': 'ticket'}
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
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.DEBUG)
    logging.info("Starting up FChatClient!")
    try:
        ws = FChatClient('ws://chat.f-list.net:8722', sys.argv[1], sys.argv[2], sys.argv[3])
        ws.connect()
        ws.run_forever()
    except KeyboardInterrupt:
        ws.close()
