import os
import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import re
import math
import aiohttp
import discord
import colorlog

from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from collections import defaultdict

from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable

from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import Player
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .constructs import SkipState, Response, VoiceStateUpdate
from .utils import load_file, write_file, fixg, ftimedelta

from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH

load_opus_lib(None)

LOG = logging.getLogger(__name__)


class MusicBot(discord.Client):
    """ TODO """
    def __init__(self, config_file=None, perms_file=None):
        if config_file is None:
            config_file = ConfigDefaults.config_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)
        self.permissions = Permissions(
            perms_file, grant_all=[self.config.owner_id])

        self.timeout = self.config.timeout

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.autoplaylist_file)
        self.banned = set(load_file(self.config.banned_file))

        self.song_list = self.autoplaylist[:]

        self.downloader = downloader.Downloader(download_folder=AUDIO_CACHE_PATH)
        self.aiolocks = defaultdict(asyncio.Lock)

        self._setup_logging()

        if not self.autoplaylist:
            LOG.warning("Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False
        else:
            LOG.info("Loaded autoplaylist with %s entries",
                     len(self.autoplaylist))

        if self.banned:
            LOG.debug("Loaded banned list with %s entries", len(self.banned))

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    def __del__(self):
        # These functions return futures but it doesn't matter
        try:
            self.http.session.close()
        except:
            pass

        try:
            self.aiosession.close()
        except:
            pass

    # TODO: Add some sort of `denied` argument for a message to send when
    # someone else tries to use it
    def owner_only(func):
        """ TODO """
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            """ TODO """
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError(
                    "only the owner can use this command", expire_in=30)

        return wrapper

    def dev_only(func):
        """ TODO """
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            """ TODO """
            orig_msg = _get_variable('message')

            if orig_msg.author.id in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError(
                    "only dev users can use this command", expire_in=30)

            wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        """ TODO """
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            """ TODO """
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
        return discord.utils.find(
            lambda m: m.id == self.config.owner_id and (
                m.voice_channel if voice else True),
            server.members if server else self.get_all_members()
        )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            LOG.debug("Skipping logger setup, already setup")
            return

        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt={
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors={
                'DEBUG':    'cyan',
                'INFO':     'white',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY':      'white',
                'FFMPEG':     'bold_purple',
                'VOICEDEBUG': 'purple',
            },
            style='{',
            datefmt=''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        LOG.debug("Set logging level to %s", self.config.debug_level_str)

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(
                filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter(
                '{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.Channel, *,
                        excluding_me=True, excluding_deaf=False):
        def check(member):
            """ TODO """
            if excluding_me and member == vchannel.server.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        return not sum(1 for m in vchannel.voice_members if check(m))

    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.server: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                LOG.info("Initial autopause in empty channel")

                player.pause()
                self.server_data[
                    player.voice_client.channel.server]['auto_paused'] = True

        for server in self.servers:
            if server.unavailable or server in channel_map:
                continue

            if server.me.voice_channel:
                LOG.info(
                    "Found resumable voice channel {0.server.name}/{0.name}"
                    .format(server.me.voice_channel))
                channel_map[server] = server.me.voice_channel

            if autosummon:
                owner = self._get_owner(server=server, voice=True)
                if owner:
                    LOG.info("Found owner in \"%s\"",
                             owner.voice_channel.name)
                    channel_map[server] = owner.voice_channel

        for server, channel in channel_map.items():
            if server in joined_servers:
                LOG.info(
                    "Already joined a channel in \"%s\", skipping",
                    server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                LOG.info(
                    "Attempting to join {0.server.name}/{0.name}"
                    .format(channel))

                chperms = channel.permissions_for(server.me)

                if not chperms.connect:
                    LOG.info(
                        "Cannot join channel \"%s\", no permission.",
                        channel.name)
                    continue

                elif not chperms.speak:
                    LOG.info(
                        "Will not join channel \"%s\", no permission to speak.",
                        channel.name)
                    continue

                try:
                    player = await self.get_player(
                        channel,
                        create=True,
                        deserialize=self.config.persistent_queue)
                    joined_servers.add(server)

                    LOG.info("Joined {0.server.name}/{0.name}".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist and not \
                            player.playlist.entries:
                        await self.on_player_finished_playing(player)

                    if self.config.auto_pause:
                        player.once('play',
                                    lambda player,
                                           **_: _autopause(player))

                except Exception:
                    LOG.debug(
                        "Error joining {0.server.name}/{0.name}"
                        .format(channel), exc_info=True)
                    LOG.debug(
                        "Failed to join {0.server.name}/{0.name}"
                        .format(channel))

            elif channel:
                LOG.warning(
                    """Not joining {0.server.name}/{0.name},
                    that's a text channel."""
                    .format(channel))

            else:
                LOG.warning("Invalid channel thing: %s", channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    # TODO: Check to see if I can just move this to on_message after the
    # response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        voice_channel = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice
        # channel
        if not voice_channel or voice_channel == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel \
                (%s)" % voice_channel.name,
                expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            LOG.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info

    async def add_to_autoplaylist(self,
                                  song_url: str,
                                  *,
                                  ex: Exception = None,
                                  write_to_apl=False):
        """ TODO """
        if song_url in self.autoplaylist:
            LOG.debug(
                "URL \"%s\" in autoplaylist, ignoring", song_url)
            return False

        async with self.aiolocks[self.add_to_autoplaylist.__name__]:
            LOG.info("Adding song to autoplaylist: %s", song_url)
            self.autoplaylist.append(song_url)

            with open(self.config.autoplaylist_log_file, 'a',
                      encoding='utf8') as file_:
                file_.write(
                    '# Entry added {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        # 10 spaces to line up with # Reason:
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10),
                        url=song_url,
                        sep='#' * 32
                    ))

            if write_to_apl:
                LOG.info("Updating autoplaylist")
                write_file(self.config.autoplaylist_file, self.autoplaylist)
                self.autoplaylist = load_file(self.config.autoplaylist_file)
                return True

    async def remove_from_autoplaylist(self,
                                       song_url: str,
                                       *,
                                       ex: Exception = None,
                                       write_to_apl=False):
        """ TODO """
        if song_url not in self.autoplaylist:
            LOG.debug(
                "URL \"%s\" not in autoplaylist, ignoring", song_url)
            return False

        async with self.aiolocks[self.remove_from_autoplaylist.__name__]:
            self.autoplaylist.remove(song_url)
            LOG.info("Removing song from autoplaylist: %s", song_url)

            with open(self.config.autoplaylist_log_file, 'a',
                      encoding='utf8') as file_:
                file_.write(
                    '# Entry removed {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        # 10 spaces to line up with # Reason:
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10),
                        url=song_url,
                        sep='#' * 32
                    ))

            if write_to_apl:
                LOG.info("Updating autoplaylist")
                write_file(self.config.autoplaylist_file, self.autoplaylist)
                self.autoplaylist = load_file(self.config.autoplaylist_file)
                return True

    @ensure_appinfo
    async def generate_invite_link(self,
                                   *,
                                   permissions=discord.Permissions(70380544),
                                   server=None):
        """ TODO """
        return discord.utils.oauth_url(
            self.cached_app_info.id, permissions=permissions, server=server)

    async def join_voice_channel(self, channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise discord.InvalidArgument(
                "Channel passed must be a voice channel")

        server = channel.server

        if self.is_voice_connected(server):
            raise discord.ClientException(
                "Already connected to a voice channel in this server")

        def session_id_found(data):
            """ TODO """
            user_id = data.get('user_id')
            guild_id = data.get('guild_id')
            return user_id == self.user.id and guild_id == server.id

        LOG.voicedebug("(%s) creating futures",
                       self.join_voice_channel.__name__)
        # register the futures for waiting
        session_id_future = self.ws.wait_for(
            'VOICE_STATE_UPDATE', session_id_found)
        voice_data_future = self.ws.wait_for(
            'VOICE_SERVER_UPDATE', lambda d: d.get('guild_id') == server.id)

        # "join" the voice channel
        LOG.voicedebug("(%s) setting voice state",
                       self.join_voice_channel.__name__)
        await self.ws.voice_state(server.id, channel.id)

        LOG.voicedebug("(%s) waiting for session id",
                       self.join_voice_channel.__name__)
        session_id_data = await asyncio.wait_for(
            session_id_future, timeout=self.timeout, loop=self.loop)

        # sometimes it gets stuck on this step.  Jake said to wait
        # indefinitely.  To hell with that.
        LOG.voicedebug("(%s) waiting for voice data",
                       self.join_voice_channel.__name__)
        data = await asyncio.wait_for(
            voice_data_future, timeout=self.timeout, loop=self.loop)

        kwargs = {
            'user': self.user,
            'channel': channel,
            'data': data,
            'loop': self.loop,
            'session_id': session_id_data.get('session_id'),
            'main_ws': self.ws
        }

        voice = discord.VoiceClient(**kwargs)
        try:
            LOG.voicedebug("(%s) connecting...",
                           self.join_voice_channel.__name__)
            with aiohttp.Timeout(self.timeout):
                await voice.connect()

        except asyncio.TimeoutError as error:
            LOG.voicedebug("(%s) connection failed, disconnecting",
                           self.join_voice_channel.__name__)
            try:
                await voice.disconnect()
            except:
                pass
            raise error

        LOG.voicedebug("(%s) connection successful",
                       self.join_voice_channel.__name__)

        self.connection._add_voice_client(server.id, voice)
        return voice

    async def get_voice_client(self, channel: discord.Channel):
        """ TODO """
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError("Channel passed must be a voice channel")

        async with self.aiolocks[
            self.get_voice_client.__name__ + ':' + channel.server.id]:
            if self.is_voice_connected(channel.server):
                return self.voice_client_in(channel.server)

            voice_channel = None
            timer0 = timer1 = 0
            tries = 5

            for attempt in range(1, tries + 1):
                LOG.debug("Connection attempt %s to %s",
                          attempt, channel.name)
                timer0 = time.time()

                try:
                    voice_channel = await self.join_voice_channel(channel)
                    timer1 = time.time()
                    break

                except asyncio.TimeoutError:
                    LOG.warning(
                        "Failed to connect, retrying (%s/%s)",
                        attempt, tries)

                    # TODO: figure out if I need this or not
                    # try:
                    #     await self.ws.voice_state(channel.server.id, None)
                    # except:
                    #     pass

                except:
                    LOG.exception(
                        "Unknown error attempting to connect to voice")

                await asyncio.sleep(0.5)

            if not voice_channel:
                LOG.critical(
                    "Voice client is unable to connect, restarting...")
                await self.restart()

            LOG.debug("Connected in %ss", timer1 - timer0)
            LOG.info("Connected to %s/%s", channel.server, channel)

            voice_channel.ws._keep_alive.name = "VoiceClient Keepalive"

            return voice_channel

    async def reconnect_voice_client(self, server, *, sleep=0.1, channel=None):
        """ TODO """
        LOG.debug("Reconnecting voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

        async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
            voice_client = self.voice_client_in(server)

            if not (voice_client or channel):
                return

            _paused = False
            player = self.get_player_in(server)

            if player and player.is_playing:
                LOG.voicedebug(
                    "(%s) Pausing", self.reconnect_voice_client.__name__)

                player.pause()
                _paused = True

            LOG.voicedebug("(%s) Disconnecting",
                           self.reconnect_voice_client.__name__)

            if self.is_voice_connected(server):
                try:
                    await voice_client.disconnect()
                except:
                    # pass
                    raise discord.ClientException(
                        "Could not disconnect from voice channel on server: {}#{}".format(server.id, server.name))

            if sleep:
                LOG.voicedebug("(%s) Sleeping for %s",
                               self.reconnect_voice_client.__name__, sleep)
                await asyncio.sleep(sleep)

            try:
                if player:
                    LOG.voicedebug("(%s) Getting voice client",
                                   self.reconnect_voice_client.__name__)

                    if not channel:
                        new_vc = await self.get_voice_client(voice_client.channel)
                    else:
                        new_vc = await self.get_voice_client(channel)

                    LOG.voicedebug("(%s) Swapping voice client",
                                   self.reconnect_voice_client.__name__)
                    await player.reload_voice(new_vc)

                    if player.is_paused and _paused:
                        LOG.voicedebug("Resuming")
                        player.resume()
            # If there is an error restart the server
            except:
                await self.disconnect_all_voice_clients()
                raise exceptions.RestartSignal

        LOG.debug("Reconnected voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

    async def disconnect_voice_client(self, server):
        """ TODO """
        voice_client = self.voice_client_in(server)
        if not voice_client:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await voice_client.disconnect()

    async def disconnect_all_voice_clients(self):
        """ TODO """
        for voice_client in list(self.voice_clients).copy():
            await self.disconnect_voice_client(voice_client.channel.server)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        """ TODO """
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError("Channel passed must be a voice channel")

        await self.ws.voice_state(vchannel.server.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, server: discord.Server) -> Player:
        """ TODO """
        return self.players.get(server.id)

    async def get_player(self, channel, create=False, *,
                         deserialize=False) -> Player:
        """ TODO """
        server = channel.server

        async with self.aiolocks[self.get_player.__name__ + ':' + server.id]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(server, voice_client)

                if player:
                    LOG.debug(
                        "Created player via deserialization for server %s with %s entries", \
                        server.id, len(player.playlist))
                    # Since deserializing only happens when the bot starts, I
                    # should never need to reconnect
                    return self._init_player(player, server=server)

            if server.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        "The bot is not in a voice channel. "
                        "Use %ssummon to summon it to your voice channel."
                        % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = Player(self, voice_client, playlist)
                self._init_player(player, server=server)

            async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
                if self.players[server.id].voice_client not in \
                        self.voice_clients:
                    LOG.debug(
                        "Reconnect required for voice client in %s", server.name)
                    await self.reconnect_voice_client(server, channel=channel)

        return self.players[server.id]

    def _init_player(self, player, *, server=None):
        player = \
            player.on('play', self.on_player_play) \
            .on('resume', self.on_player_resume) \
            .on('pause', self.on_player_pause) \
            .on('stop', self.on_player_stop) \
            .on('finished-playing', self.on_player_finished_playing) \
            .on('entry-added', self.on_player_entry_added) \
			.on('entry-removed', self.on_player_entry_removed) \
            .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if server:
            self.players[server.id] = player

        return player

    async def on_player_play(self, player, entry):
        """ TODO """
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.server)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)
        thumbnail = entry.filename_thumbnail

        if channel and author:
            if self.config.now_playing_mentions:
                newmsg = "%s - your song **%s** is now playing in %s!" % (
                    entry.meta['author'].mention,
                    entry.title,
                    player.voice_client.channel.name)
            else:
                newmsg = "Now playing in %s: **%s**" % (
                    player.voice_client.channel.name, entry.title)

            if self.server_data[channel.server]['last_np_msg']:
                self.server_data[channel.server]['last_np_msg'] = \
                    await self.safe_edit_message(
                        self.server_data[channel.server]['last_np_msg'],
                        newmsg,
                        fp=thumbnail,
                        send_if_fail=True)
            elif thumbnail and self.config.show_thumbnails:
                self.server_data[channel.server]['last_np_msg'] = \
                    await self.safe_send_file(channel, newmsg, thumbnail)
            else:
                self.server_data[channel.server]['last_np_msg'] = \
                    await self.safe_send_message(channel, newmsg)

    # TODO: Check channel voice state?
    async def on_player_resume(self, player, entry, **_):
        """ TODO """
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        """ TODO """
        await self.update_now_playing_status(entry, True)
        # await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_stop(self, player, **_):
        """ TODO """
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        """ TODO """
        # Played song message deletion
        entry = player.previous_entry
        if entry is not None:
            channel = entry.meta.get('channel', None)
            author = entry.meta.get('author', None)

            if channel and author:
                last_np_msg = \
                    self.server_data[channel.server]['last_np_msg']
                if last_np_msg and last_np_msg.channel == channel:
                    async for lmsg in self.logs_from(channel, limit=1):
                        if lmsg != last_np_msg and last_np_msg:
                            await self.safe_delete_message(last_np_msg)
                            self.server_data[channel.server]['last_np_msg'] = None
                        break  # This is probably redundant

        # Auto playlist add song
        if not player.playlist.entries and not player.current_entry and \
                self.config.auto_playlist:
            while self.autoplaylist:
                random.shuffle(self.autoplaylist)
                if len(self.song_list) == 0:
                    LOG.debug(
                        """All songs have been played.
                        Restarting auto playlist...""")
                    self.song_list = self.autoplaylist[:]
                song_url = random.choice(self.song_list)
                self.song_list.remove(song_url)

                info = {}

                try:
                    info = await self.downloader.extract_info(
                        player.playlist.loop,
                        song_url,
                        download=False,
                        process=False)
                except downloader.youtube_dl.utils.DownloadError as error:
                    if 'YouTube said:' in error.args[0]:
                        # url is bork, remove from list and put in removed list
                        LOG.debug(
                            "Error processing youtube url:\n%s", error.args[0])

                    else:
                        # Probably an error from a different extractor, but
                        # I've only seen youtube's
                        LOG.debug("Error processing \"%(url)s\": %(ex)s", song_url, error)

                    await self.remove_from_autoplaylist(song_url,
                                                        ex=error,
                                                        write_to_apl=True)
                    continue

                except Exception as error:
                    LOG.debug("Error processing \"%(url)s\": %(ex)s", song_url, error)
                    LOG.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                # or if info.get('_type', '') == 'playlist'
                if info.get('entries', None):
                    LOG.debug("""Playlist found but is unsupported at this time
                    , skipping.""")
                    # TODO: Playlist expansion

                # Do I check the initial conditions again?
                # not (not player.playlist.entries and not
                # player.current_entry and self.config.auto_playlist)

                try:
                    await player.playlist.add_entry(
                        song_url, channel=None, author=None)
                except exceptions.ExtractionError as error:
                    LOG.error(
                        "Error adding song from autoplaylist: %s", error)
                    LOG.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                # TODO: When I add playlist expansion, make sure that's not
                # happening during this check
                LOG.warning(
                    "No playable songs in the autoplaylist, disabling.")
                self.config.auto_playlist = False

        else:  # Don't serialize for autoplaylist events
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        """ TODO """
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_entry_removed(self, player, playlist, entry, **_):
        """ TODO """
        LOG.debug("TODO implement on_player_entry_removed")

    async def on_player_error(self, player, entry, ex, **_):
        """ TODO """
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            LOG.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        """ TODO """
        if self.config.now_playing_status:
            game = None

            if self.user.bot:
                activeplayers = sum(
                    1 for p in self.players.values() if p.is_playing)
                if activeplayers > 1:
                    game = discord.Game(name="music on %s servers" % activeplayers)
                    entry = None

                elif activeplayers == 1:
                    player = discord.utils.get(
                        self.players.values(), is_playing=True)
                    entry = player.current_entry

            if entry:
                prefix = u'\u275A\u275A ' if is_paused else ''

                name = u'{}{}'.format(prefix, entry.title)[:128]
                game = discord.Game(name=name)

            async with self.aiolocks[self.update_now_playing_status.__name__]:
                if game != self.last_status:
                    await self.change_presence(game=game)
                    self.last_status = game

    async def update_now_playing_message(self,
                                         server,
                                         message,
                                         *,
                                         channel=None):
        """ TODO """
        lnp = self.server_data[server]['last_np_msg']
        message = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp:  # If there was a previous lp message
            oldchannel = lnp.channel

            # If we have a channel to update it in
            if lnp.channel == oldchannel:
                async for lmsg in self.logs_from(channel, limit=1):
                    # If we need to resend it
                    if lmsg != lnp and lnp:
                        await self.safe_delete_message(lnp, quiet=True)
                        message = await self.safe_send_message(
                            channel, message, quiet=True)
                    else:
                        message = await self.safe_edit_message(
                            lnp, message, send_if_fail=True, quiet=False)

            elif channel:  # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                message = await self.safe_send_message(channel, message, quiet=True)

            else:  # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                message = await self.safe_send_message(
                    oldchannel, message, quiet=True)

        elif channel:  # No previous message
            message = await self.safe_send_message(channel, message, quiet=True)

        self.server_data[server]['last_np_msg'] = message

    async def serialize_queue(self, server, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(server)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            LOG.debug("Serializing queue for %s", server.id)

            with open(dir, 'w', encoding='utf8') as file_:
                file_.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        """ TODO """
        coros = [self.serialize_queue(s, dir=dir) for s in self.servers]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self,
                                server,
                                voice_client,
                                playlist=None,
                                *,
                                dir=None) -> Player:
        """
            Deserialize a saved queue for a server into a Player.
            If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            if not os.path.isfile(dir):
                return None

            LOG.debug("Deserializing queue for %s", server.id)

            with open(dir, 'r', encoding='utf8') as file_:
                data = file_.read()
            
            if self.config.save_videos is False:
                 data = re.sub('"downloaded": true,', '"downloaded": false,', data)

        return Player.from_json(data, self, voice_client, playlist)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # playlists in autoplaylist
        await self._scheck_autoplaylist()

        # config/permissions async validate?
        await self._scheck_configs()

    async def _scheck_ensure_env(self):
        LOG.debug("Ensuring data folders exist")
        for server in self.servers:
            pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as file_:
            for server in sorted(self.servers, key=lambda s: int(s.id)):
                file_.write('{:<22} {}\n'.format(server.id, server.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                LOG.debug("Deleted old audio cache")
            else:
                LOG.debug("Could not delete old audio cache, moving on.")

    async def _scheck_server_permissions(self):
        LOG.debug("Checking server permissions")
        pass  # TODO

    async def _scheck_autoplaylist(self):
        LOG.debug("Auditing autoplaylist")
        pass  # TODO

    async def _scheck_configs(self):
        LOG.debug("Validating config")
        await self.config.async_validate(self)

        LOG.debug("Validating permissions config")
        await self.permissions.async_validate(self)
##########################################################################

    async def safe_send_message(self, dest, content, **kwargs):
        """ TODO """
        tts = kwargs.pop('tts', False)
        quiet = kwargs.pop('quiet', False)
        expire_in = kwargs.pop('expire_in', 0)
        allow_none = kwargs.pop('allow_none', True)
        also_delete = kwargs.pop('also_delete', None)

        msg = None
        lfunc = LOG.debug if quiet else LOG.warning

        try:
            if content is not None or allow_none:
                msg = await self.send_message(dest, content, tts=tts)

        except discord.Forbidden:
            lfunc("Cannot send message to \"%s\", no permission", dest.name)

        except discord.NotFound:
            lfunc("Cannot send message to \"%s\", invalid channel?", dest.name)

        except discord.HTTPException:
            if len(content) > DISCORD_MSG_CHAR_LIMIT:
                lfunc("Message is over the message size limit (%s)",
                      DISCORD_MSG_CHAR_LIMIT)
            else:
                lfunc("Failed to send message")
                LOG.noise(
                    "Got HTTPException trying to send message to %s: %s",
                    dest,
                    content)

        finally:
            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(
                    self._wait_delete_msg(also_delete, expire_in))

        return msg

    async def safe_send_file(self,
                             dest,
                             content,
                             fp,
                             *,
                             tts=False,
                             expire_in=0,
                             also_delete=None,
                             quiet=False,
                             filename=None):
        """ TODO """
        msg = None
        try:
            msg = await self.send_file(dest, fp, content=content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(
                    self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print(
                    """Warning: Cannot send message or file to %s,
                    no permission""" % dest.name)

        except discord.NotFound:
            if not quiet:
                self.safe_print(
                    """Warning: Cannot send message or file to %s,
                    invalid channel?""" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        """ TODO """
        lfunc = LOG.debug if quiet else LOG.warning

        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            lfunc("Cannot delete message \"{}\", no permission".format(
                message.clean_content))

        except discord.NotFound:
            lfunc("Cannot delete message \"{}\", message not found".format(
                message.clean_content))

    async def safe_edit_message(self,
                                message,
                                new,
                                *,
                                fp=None,
                                send_if_fail=False,
                                quiet=False):
        """ TODO """
        lfunc = LOG.debug if quiet else LOG.warning

        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            lfunc("Cannot edit message \"{}\", message not found".format(
                message.clean_content))
            if send_if_fail:
                lfunc("Sending message instead")
                if fp and self.config.show_thumbnails:
                    return await self.safe_send_file(message.channel, new, fp)
                else:
                    return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            LOG.warning(
                "Could not send typing to %s, no permission",
                destination)

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)

    async def restart(self):
        """ TODO """
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        """ TODO """
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except:
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your %s in the config.ini file.  "
                "Remember that each field should be on their own line."
                % ['Token', 'Credentials'][len(self.config.auth)]
            )  # ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                LOG.error("Error in cleanup", exc_info=True)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            LOG.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            LOG.error("Exception in %s", event, exc_info=True)

    async def on_resumed(self):
        """ TODO """
        LOG.info("\nReconnected to discord.\n")

    async def on_ready(self):
        """ TODO """
        dlogger = logging.getLogger('discord')
        for handler in dlogger.handlers:
            if getattr(handler, 'terminator', None) == '':
                dlogger.removeHandler(handler)
                print()

        LOG.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            LOG.debug(
                "Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()
        print()

        LOG.info('Connected!   -   MusicBot v%s\n', BOTVERSION)

        self.init_ok = True

        ################################

        LOG.info("Bot:   {0}/{1}#{2}{3}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator,
            ' [BOT]' if self.user.bot else ' [Userbot]'
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            LOG.info("Owner: {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            LOG.info('Server List:')
            [LOG.info(' - ' + s.name) for s in self.servers]

        elif self.servers:
            LOG.warning(
                "Owner could not be found on any server (id: %s)\n",
                self.config.owner_id)

            LOG.info('Server List:')
            [LOG.info(' - ' + s.name) for s in self.servers]

        else:
            LOG.warning("Owner unknown, bot is not on any servers.")
            if self.user.bot:
                LOG.warning(
                    """To make the bot join a server,
                    paste this link in your browser. \n
                    Note: You should be logged into your main account
                    and have \n
                    manage server permissions on the server you want
                    the bot to join.\n"""
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i)
                         for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type ==
                            discord.ChannelType.voice)

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                LOG.info("Bound to text channels:")
                [LOG.info(' - {}/{}'
                          .format(ch.server.name.strip(),
                                  ch.name.strip())) for ch in chlist if ch]
            else:
                LOG.info("Not bound to any text channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                LOG.info("Not binding to voice channels:")
                [LOG.info(' - {}/{}'
                          .format(ch.server.name.strip(),
                                  ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            LOG.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i)
                         for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type ==
                            discord.ChannelType.text)

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                LOG.info("Autojoining voice channels:")
                [LOG.info(' - {}/{}'
                          .format(ch.server.name.strip(),
                                  ch.name.strip())) for ch in chlist if ch]
            else:
                LOG.info("Not autojoining any voice channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                LOG.info("Can not autojoin text channels:")
                [LOG.info(' - {}/{}'
                          .format(ch.server.name.strip(),
                                  ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            LOG.info("Not autojoining any voice channels")
            autojoin_channels = set()

        print(flush=True)
        LOG.info("Options:")

        LOG.info("  Command prefix: " + self.config.command_prefix)
        LOG.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
        LOG.info("  Maximum volume: {}%".format(self.config.max_volume))
        LOG.info("  Skip threshold: {} votes or {}%".format(
            self.config.skips_required,
            fixg(self.config.skip_ratio_required * 100)))
        LOG.info("  Time out: %s", self.config.timeout)
        LOG.info("  Downloaded songs will be: " +
                 ['Deleted', 'Saved'][self.config.save_videos])
        LOG.info("  Show Thumbnails: " +
                 ['Disabled', 'Enabled'][self.config.show_thumbnails])
        LOG.info("  Now Playing @mentions: " +
                 ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        LOG.info("  Auto-Summon: " +
                 ['Disabled', 'Enabled'][self.config.auto_summon])
        LOG.info("  Auto-Playlist: " +
                 ['Disabled', 'Enabled'][self.config.auto_playlist])
        LOG.info("  Auto-Pause: " +
                 ['Disabled', 'Enabled'][self.config.auto_pause])
        LOG.info("  Delete Messages: " +
                 ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            LOG.info("  Delete Invoking: " +
                     ['Disabled', 'Enabled'][self.config.delete_invoking])
        LOG.info("  Persistent Queue: " +
                 ['Disabled', 'Enabled'][self.config.persistent_queue])
        LOG.info("  Debug Mode: " +
                 ['Disabled', 'Enabled'][self.config.debug_mode])
        LOG.info("  Debug Level: " + self.config.debug_level_str)
        print(flush=True)

        # maybe option to leave the ownerid blank and generate a random command
        # for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(
            autojoin_channels, autosummon=self.config.auto_summon)

        # t-t-th-th-that's all folks!

    async def cmd_help(self, command=None):
        """
        Usage:
            {command_prefix}help
            {command_prefix}help [command]

        Prints a help message.
        If a command is specified, it prints a help message for that command.
        Otherwise, it lists the available commands.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd and not hasattr(cmd, 'dev_cmd'):
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__)
                    ).format(command_prefix=self.config.command_prefix),
                    delete_after=60
                )
            else:
                return Response("No such command", delete_after=10)

        else:
            helpmsg = "**Available commands**\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help' \
                        and not hasattr(getattr(self, att), 'dev_cmd'):
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(
                        self.config.command_prefix, command_name))

            commands.sort()
            helpmsg += ", ".join(commands)
            helpmsg += "```\n <https://github.com/DiscordMusicBot/MusicBot/wiki/Commands>"
            helpmsg += "\n\nYou can also use `{}help x` for more info about each command." \
            .format(self.config.command_prefix)

            return Response(helpmsg, reply=True, delete_after=60)

    async def cmd_blacklist(self, option, song_url):
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] url

        Add or remove song URL to the blacklist.
        """

        song_url = song_url.strip('<>')

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                "Invalid option \"%s\" specified, use +, -, add, or remove" % option, expire_in=20
            )

        if option in ['+', 'add']:
            self.blacklist.update(song_url)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                "%s has been added to the blacklist." % (song_url),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(song_url):
                return Response("The song is not blacklisted.", reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(song_url)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    "%s has been removed from the blacklist." % (song_url),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id
            {command_prefix}id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response("your id is `%s`" % author.id,
                            reply=True,
                            delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id),
                            reply=True,
                            delete_after=35)

    @owner_only
    async def cmd_ban(self, option, user_mentions):
        """
        Usage:
            {command_prefix}ban [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the banned list.
        Banned users are forbidden from using bot.
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                "Invalid option \"%s\" specified, use +, -, add, or remove" %
                option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                LOG.info("[Commands:ban] The owner cannot be banned.")
                user_mentions.remove(user)

        old_len = len(self.banned)

        if option in ['+', 'add']:
            self.banned.update(user.id for user in user_mentions)

            write_file(self.config.banned_file, self.banned)

            return Response(
                "%s users have been added to the banned list" % (
                    len(self.banned) - old_len),
                reply=True,
                delete_after=10
            )

        else:
            if self.banned.isdisjoint(user.id for user in user_mentions):
                return Response(
                    "None of those users are in the banned.",
                    reply=True,
                    delete_after=10)

            else:
                self.banned.difference_update(
                    user.id for user in user_mentions)
                write_file(self.config.banned_file, self.banned)

                return Response(
                    "%s users have been removed from the banned" % (
                        old_len - len(self.banned)),
                    reply=True,
                    delete_after=10
                )

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        Usage:
            {command_prefix}joinserver invite_link

        Asks the bot to join a server.
        Note: Bot accounts cannot use invite links.
        """

        if self.user.bot:
            url = await self.generate_invite_link()
            return Response(
                """Bot accounts can't use invite links!\n
                Click here to add me to a server: \n{}""".format(url),
                reply=True,
                delete_after=30
            )

        try:
            if server_link:
                await self.accept_invite(server_link)
                return Response("\n{THUMBS UP SIGN}")

        except:
            raise exceptions.CommandError(
                "Invalid URL provided:\n{}\n".
                format(server_link), expire_in=30)

    async def cmd_broadcast(self, args, leftover_args):
        """
        Usage:
            {command_prefix}broadcast message

        Broadcasts a message to all servers default channels.
        """
        if leftover_args:
            args = ' '.join([args, *leftover_args])

        for s in self.servers:
            await self.safe_send_message(s, args)

    async def cmd_play(self, player, channel, author, permissions,
                       leftover_args, song_url):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for

        Adds the song to the playlist. If a link is not provided, the first
        result from a youtube search is added to the queue.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) \
                >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" %
                permissions.max_songs,
                expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        linksRegex = r'((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)

        if matchUrl is None:
            song_url = song_url.replace('/', '%2F')

        try:
            info = await self.downloader.extract_info(
                player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError(
                "That video cannot be played. Try using the {}stream command."
                .format(self.config.command_prefix),
                expire_in=30
            )

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # LOG.info("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(
                        channel, "```\n%s\n```" % e, expire_in=120),
                    loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    "Error extracting info from search string, youtubedl \
                    returned no data.  "
                    "You may need to restart the bot if this continues \
                    to happen.", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                LOG.debug("Got empty list, no data")
                return

            # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(
                player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player,
            # channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things
        # like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no
        # processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an
            # arbitrary number of search results
            if not permissions.allow_playlists and ':search' in \
                    info['extractor'] and len(info['entries']) > 1:
                raise exceptions.PermissionsError(
                    "You are not allowed to request playlists", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is
            # if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > \
                    permissions.max_playlist_length:
                raise exceptions.PermissionsError(
                    "Playlist has too many entries (%s > %s)" % (
                        num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add
            # the other check back in
            if permissions.max_songs and \
                    player.playlist.count_for_user(author) + num_songs > \
                    permissions.max_songs:
                raise exceptions.PermissionsError(
                    "Playlist entries + your already queued songs reached \
                    limit (%s + %s > %s)" % (
                        num_songs,
                        player.playlist.count_for_user(author),
                        permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist',
                                             'soundcloud:set',
                                             'bandcamp:album']:
                try:
                    return await self._cmd_play_playlist_async(
                        player, channel, author, permissions,
                        song_url, info['extractor'])
                except exceptions.CommandError:
                    raise
                except Exception as e:
                    LOG.error("Error queuing playlist", exc_info=True)
                    raise exceptions.CommandError(
                        "Error queuing playlist:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge
            # it a bit, unless we can monitor it and edit the message with the
            # estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, this will have to do.
            # It would probably be a thread to check a few playlists and
            # get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Gathering playlist information for {} songs{}'
                .format(num_songs, ', ETA: {} seconds'
                        .format(fixg(num_songs * wait_per_song)) if
                        num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.
            # We need either a loop that sends these every 10 seconds
            # or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead,
            # add event functions, and every play list might be asyncified
            # Also have a "verify_entry" hook with the entry as an arg and
            # returns the entry if its ok

            entry_list, position = await player.playlist.import_from(
                song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this
                        # would ever break. Unless the first entry starts
                        # being played, which would make this a race condition
                if drop_count:
                    LOG.info("Dropped %s songs", drop_count)

            LOG.info(
                "Processed {} songs in {} seconds at {:.2f}s/song, \
            {:+.2g}/song from expected ({}s)"
                .format(
                    listlen,
                    fixg(ttime),
                    ttime / listlen if listlen else 0,
                    ttime / listlen - wait_per_song if listlen -
                    wait_per_song else 0,
                    fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise exceptions.CommandError(
                    "No songs were added, all songs were over max duration \
                    (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "Enqueued **%s** songs to be played. \
            Position in queue: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > \
                    permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (
                        info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(
                    song_url, channel=channel, author=author)

            except exceptions.WrongEntryTypeError as error:
                if error.use_url == song_url:
                    LOG.warning(
                        "Determined incorrect entry type, but suggested url \
                        is the same. Help.")

                LOG.debug(
                    "Assumed url \"%s\" was a single entry, was actually a \
                    playlist", song_url)
                LOG.debug("Using \"%s\" instead", error.use_url)

                return await self.cmd_play(
                    player, channel, author, permissions,
                    leftover_args, e.use_url)

            reply_text = "Enqueued **%s** to be played.\nPosition in queue: %s"
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Up next!'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(
                    position, player)
                reply_text += ' - estimated time until playing: %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, ftimedelta(time_until))

        return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self,
                                       player,
                                       channel,
                                       author,
                                       permissions,
                                       playlist_url,
                                       extractor_type):
        """
        Secret handler to use the async wizardry to make playlist
        queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(
            player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            # TODO: From playlist_title
            channel, "Processing %s songs..." % num_songs)
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = \
                    await player.playlist.async_process_youtube_playlist(
                        playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                LOG.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(
                    'Error handling playlist %s queuing.',
                    playlist_url,
                    expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = \
                    await player.playlist.async_process_sc_bc_playlist(
                        playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                LOG.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(
                    'Error handling playlist %s queuing.',
                    playlist_url,
                    expire_in=30)

        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                LOG.debug("Dropped %s songs", drop_count)

            if player.current_entry and player.current_entry.duration > \
                    permissions.max_song_length:
                await self.safe_delete_message(
                    self.server_data[channel.server]['last_np_msg'])
                self.server_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and
        # return that too

        # This is technically inaccurate since bad songs are ignored but still
        # take up time
        LOG.info(
            "Processed {}/{} songs in {} seconds at {:.2f}s/song, \
        {:+.2g}/song from expected ({}s)"
            .format(
                songs_processed,
                num_songs,
                fixg(ttime),
                ttime / num_songs if num_songs else 0,
                ttime / num_songs - wait_per_song if num_songs -
                wait_per_song else 0, fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over \
            max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped \
                for being too long."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("Enqueued {} songs to be played in {} seconds".format(
            songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):
        """
        Usage:
            {command_prefix}stream song_link

        Enqueue a media stream.
        This could mean an actual stream like Twitch or shoutcast,
        or simply streaming media without predownloading it.
        Note: FFmpeg is notoriously bad at handling streams, especially on
        poor connections. You have been warned!
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) \
                >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" %
                permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(
            song_url, channel=channel, author=author)

        return Response(":+1:", delete_after=6)

    async def cmd_search(self,
                         player,
                         channel,
                         author,
                         permissions,
                         leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query

        Searches a service for a video and adds it to the queue.
        - service: any one of the following services:
            - youtube (yt) (default if unspecified)
            - soundcloud (sc)
            - yahoo (yh)
        - number: return a number of results and waits for user to choose one
          - defaults to 1 if unspecified
          - note: If your search query starts with a number,
                  you must put your query in quotes
            - ex: {command_prefix}search 2 "I ran seagulls"
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > \
                permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your playlist item limit (%s)" %
                permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                # noinspection PyUnresolvedReferences
                raise exceptions.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__
                        .format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError(
                "Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 3
        # max_items can be whatever, but since ytdl uses about 1000,
        # a small number might be better
        max_items = 10
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError(
                    "You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (
            services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(
            channel, "Searching for videos...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(
                player.playlist.loop,
                search_query,
                download=False,
                process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("No videos found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format( \
                    self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(
                channel, "Result %s/%s: %s" % (
                    info['entries'].index(e) + 1,
                    len(info['entries']),
                    e['webpage_url']))

            confirm_message = await self.safe_send_message(
                channel, "Is this ok? Type `y`, `n` or `exit`")
            response_message = await self.wait_for_message(
                30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("Ok nevermind.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(
                    self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

                await self.cmd_play(
                    player, channel, author, permissions, [], e['webpage_url'])

                return Response("Alright, coming right up!", delete_after=30)
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Oh well \n:slight_frown:", delete_after=30)

    async def cmd_np(self, player, channel, server, message):
        """
        Usage:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_data[server]['last_np_msg']:
                await self.safe_delete_message(
                    self.server_data[server]['last_np_msg'])
                self.server_data[server]['last_np_msg'] = None

            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(
                timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming
                        else '`[{progress}/{total}]`') \
                .format(progress=song_progress, total=song_total)
            prog_bar_str = ''
            thumbnail = player.current_entry.filename_thumbnail

            # percentage shows how much of the current song has already been
            # played
            percentage = 0.0
            if player.current_entry.duration > 0:
                percentage = player.progress / player.current_entry.duration
            """
            This for loop adds empty or full squares to prog_bar_str.
            If the song has already played 25% of the duration:
            ■■■■■■■■■■□□□□□□□□□□□□□□□□□□□□□□□□□□□□□□
            """
            progress_bar_length = 30
            for i in range(progress_bar_length):
                if percentage < 1 / progress_bar_length * i:
                    prog_bar_str += '□'
                else:
                    prog_bar_str += '■'

            action_text = 'Streaming' if streaming else 'Playing'

            if player.current_entry.meta.get('channel', False) and \
                    player.current_entry.meta.get('author', False):
                np_text = """Now {action}: **{title}** added by **{author}**
                \nProgress: {progress_bar} {progress}
                \n\n:point_right: <{url}>""" \
                .format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:
                np_text = """Now {action}: **{title}**
                \nProgress: {progress_bar} {progress}
                \n\n:point_right: <{url}>""" \
                .format(
                    action=action_text,
                    title=player.current_entry.title,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            if thumbnail and self.config.show_thumbnails:
                self.server_data[server]['last_np_msg'] = \
                    await self.safe_send_file(channel, np_text, thumbnail)
            else:
                self.server_data[server]['last_np_msg'] = \
                    await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                "There are no songs queued! Queue something with {}play."
                .format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_playlist(self, player, author, option, song_url=None):
        """
        Usage:
            {command_prefix}playlist [ + | - | add | remove ]
            {command_prefix}playlist [ + | - | add | remove ] url

        Add or remove the current song to the playlist.
        Add or remove the song based on the url to the playlist.
        """
        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                "Invalid option \"%s\" specified, use +, -, add, or remove" % option,
                expire_in=20
            )

        # No URL provided
        if song_url is None:
            # Check if there is something playing
            if not player._current_entry:
                raise exceptions.CommandError("There is nothing playing.", expire_in=20)

            # Get the URL of the current entry
            else:
                song_url = player._current_entry.url
                title = player._current_entry.title

        else:
            song_url = song_url.strip('<>')

            # Get song info from URL
            info = await self.downloader.safe_extract_info(
                player.playlist.loop, song_url, download=False, process=False)

            # Verify proper URL
            if not info:
                raise exceptions.CommandError(
                    "Error. Please insure that it is a valid YouTube, SoundCloud or BandCamp URL!",
                    expire_in=20)
            else:
                title = info.get('title', '')

        if option in ['+', 'add']:
            # Verify song is not already in the playlist
            added = False
            added = await self.add_to_autoplaylist(song_url,
                                                   ex="User: %s/%s#%s added %s" % (author.id, author.name, author.discriminator, title),
                                                   write_to_apl=True)
            if added is True:
                return Response("Added **%s** to autoplaylist." % title, delete_after=30)
            else:
                return Response("Song already present in autoplaylist.", delete_after=30)

        else:
            # Verify that the song is in our playlist
            removed = False
            removed = await self.remove_from_autoplaylist(song_url,
                                                          ex="User: %s/%s#%s removed %s" % (author.id, author.name, author.discriminator, title),
                                                          write_to_apl=True)

            if removed is True:
                return Response("Removed **%s** from the autoplaylist." % title, delete_after=30)
            else:
                return Response("Song not present in autoplaylist.", delete_after=30)

    async def cmd_summon(self, channel, server, author, voice_channel):
        """
        Usage:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """

        if not author.voice_channel:
            raise exceptions.CommandError("You are not in a voice channel!")

        voice_client = self.voice_client_in(server)
        if voice_client and server == author.voice_channel.server:
            await voice_client.move_to(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(server.me)

        if not chperms.connect:
            LOG.warning("Cannot join channel \"%s\", no permission.",
                        author.voice_channel.name)
            return Response(
                "```Cannot join channel \"%s\", no permission.```",
                author.voice_channel.name,
                delete_after=25
            )

        elif not chperms.speak:
            LOG.warning(
                "Will not join channel \"%s\", no permission to speak.",
                author.voice_channel.name)
            return Response(
                "```Will not join channel \"%s\", no permission to speak.```",
                author.voice_channel.name,
                delete_after=25
            )

        LOG.info(
            "Joining {0.server.name}/{0.name}".format(author.voice_channel))

        player = await self.get_player(
            author.voice_channel, create=True,
            deserialize=self.config.persistent_queue)

        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_pause(self, player):
        """
        Usage:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError(
                "Player is not playing.", expire_in=30)

    async def cmd_resume(self, player):
        """
        Usage:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError(
                "Player is not paused.", expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle

        Shuffles the playlist.
        """

        player.playlist.shuffle()

        cards = ['\n:spades:',
                 '\n:clubs:',
                 '\n:heart:',
                 '\n:diamonds:']
        random.shuffle(cards)

        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.8)

        await self.safe_delete_message(hand, quiet=True)
        return Response("\n:ok_hand:", delete_after=15)

    async def cmd_clear(self, player, author):
        """
        Usage:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response("\n:put_litter_in_its_place:", delete_after=20)

    async def cmd_skip(self,
                       player,
                       channel,
                       author,
                       message,
                       permissions,
                       voice_channel,
                       option=None):
        """
        Usage:
            {command_prefix}skip
            {command_prefix}skip f

        Skips the current song when enough votes are cast, or by the bot owner.
        {command_prefix}skip f will skip the current song without a vote.
        """

        if player.is_stopped:
            if player.is_stopped:
                raise exceptions.CommandError(
                    "Can't skip! The player is not playing!", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response(
                        "The next song `%s` is downloading. Please wait." %
                        player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    return Response(
                        "The next song will be played shortly. Please wait.")
                else:
                    return Response(
                        "Something odd is happening.\n"
                        "You might want to restart the bot if it does not start working.")
            else:
                return Response(
                    "Something strange is happening.\n"
                    "You might want to restart the bot if it does not start working.")

        if option is not None and option in ['f', 'force']:
            if author.id == self.config.owner_id \
                    or permissions.instaskip \
                    or author == player.current_entry.meta.get('author', None):

                player.skip()  # check autopause stuff here
                await self._manual_delete_check(message)

                return Response(
                    "your forced skip request for **{}** was acknowledged. {}".format(
                        player.current_entry.title,
                        " Next song coming up!" if player.playlist.peek() else ""
                    ),
                    reply=True,
                    delete_after=30
                )
            else:
                return Response(
                    "You do not have permission to use this command!",
                    reply=True,
                    delete_after=30
                )
        else:
            # TODO: ignore person if they're deaf or
            # take them out of the list or something?
            # Currently is recounted if they vote, deafen, then vote
            num_voice = sum(1 for m in voice_channel.voice_members if not (
                m.deaf or m.self_deaf or m.id in [self.user.id]))

            num_skips = player.skip_state.add_skipper(author.id, message)

            skips_remaining = min(
                self.config.skips_required,
                # Number of skips from config ratio
                math.ceil(self.config.skip_ratio_required / (1 / num_voice))
            ) - num_skips

            if skips_remaining <= 0:
                player.skip()  # check autopause stuff here
                return Response(
                    "your skip request for **{}** was acknowledged."
                    "\nThe vote to skip has been passed.{}".format(
                        player.current_entry.title,
                        " Next song coming up!" if player.playlist.peek() else ""
                    ),
                    reply=True,
                    delete_after=20
                )
            else:
                # TODO: When a song gets skipped, delete the old x needed to skip
                # messages
                return Response(
                    "your skip request for **{}** was acknowledged."
                    "\n**{}** more {} required to vote to skip this song.".format(
                        player.current_entry.title,
                        skips_remaining,
                        "person is" if skips_remaining == 1 else "people are"
                    ),
                    reply=True,
                    delete_after=20
                )

    async def cmd_fwd(self, player, timestamp):
        """
        Usage:
            {command_prefix}fwd <timestamp>

        Forward <timestamp> into the current entry
        E.g. 01:10
        """
        if isinstance(player.current_entry, StreamPlaylistEntry):
            return Response("It is not possible to forward a STREAM!", delete_after=20)
        else:
            parts = timestamp.split(":")
            if len(parts) < 1:  # Shouldn't occur, but who knows?
                return Response("Please provide a valid timestamp!", delete_after=20)

            # seconds, minutes, hours, days
            values = (1, 60, 60 * 60, 60 * 60 * 24)

            secs = 0
            for i in range(len(parts)):
                try:
                    v = int(parts[i])
                except:
                    continue

                j = len(parts) - i - 1
                if j >= len(values):  # If I don't have a conversion from this to seconds
                    continue

                secs += v * values[j]

            if player.current_entry is None:
                return Response("Nothing playing!", delete_after=20)

            if not player.goto_seconds(player.progress + secs):
                return Response("Timestamp exceeds song duration!", delete_after=20)

    async def cmd_rwd(self, player, timestamp):
        """
        Usage:
            {command_prefix}rwd <timestamp>

        Rewind <timestamp> into the current entry
        E.g. 01:10
        """
        if isinstance(player.current_entry, StreamPlaylistEntry):
            return Response("It is not possible to rewind a STREAM!", delete_after=20)
        else:
            parts = timestamp.split(":")
            if len(parts) < 1:  # Shouldn't occur, but who knows?
                return Response("Please provide a valid timestamp!", delete_after=20)

            # seconds, minutes, hours, days
            values = (1, 60, 60 * 60, 60 * 60 * 24)

            secs = 0
            for i in range(len(parts)):
                try:
                    v = int(parts[i])
                except:
                    continue

                j = len(parts) - i - 1
                if j >= len(values):  # If I don't have a conversion from this to seconds
                    continue

                secs += v * values[j]

            if player.current_entry is None:
                return Response("Nothing playing!", delete_after=20)

            if not player.goto_seconds(player.progress - secs):
                return Response("Timestamp exceeds song duration!", delete_after=20)


    async def cmd_volume(self,
                         author,
                         message,
                         player,
                         permissions,
                         new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]

        Sets the playback volume. Accepted values are from 1 to 100, or 1 to
        {max_volume} if you have the 'allow_higher_volume: True' permission.
        Putting + or - before the volume will make the volume change relative
        to the current volume.
        """

        # Permissions for volume over 100
        if author.id == self.config.owner_id or \
                permissions.allow_higher_volume:
            max_volume = self.config.max_volume
        else:
            max_volume = 100

        if not new_volume:
            return Response(
                "Current volume: `%s%%`" % int(player.volume * 100),
                reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError(
                "{} is not a valid number".format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= max_volume:
            player.volume = new_volume / 100.0

            return Response(
                "Updated volume from %d to %d" % (old_volume, new_volume),
                reply=True,
                delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    """Unreasonable volume change provided: {}{:+} -> {}%\n
                    Provide a change between {} and {:+}"""
                    .format(old_volume,
                            vol_change,
                            old_volume + vol_change,
                            1 - old_volume,
                            max_volume - old_volume),
                    expire_in=20)
            else:
                raise exceptions.CommandError(
                    """Unreasonable volume provided: {}%\n
                    Provide a value between 1 and {}"""
                    .format(new_volume, max_volume),
                    expire_in=20)

    async def cmd_autoplaylist(self, channel, author, player, voice_channel):
        """
        Usage:
            {command_prefix}autoplaylist

        Enable or disable the autoplaylist
        """
        self.config.auto_playlist = not self.config.auto_playlist
        await self.safe_send_message(
            channel, "The autoplaylist is now " +
            ['disabled', 'enabled'][self.config.auto_playlist])
        # if nothing is queued or playing, start a song
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_history(self, channel, player, amount=10):
        """
        Usage:
            {command_prefix}history
            {command_prefix}history number

        Prints by default the last 10 played songs.
        If a value is given then that amount will be printed.
        """
        LOG.debug("TODO IMPLEMENT cmd_history")

        # counter = 0

        # for i, item in enumerate(player.playlist, 1):
        #     if counter == amount:
        #         break
        #     elif item.meta.get('channel', False) and \
        #             item.meta.get('author', False):
        #         nextline = "`{}.` **{}** added by **{}**".format(
        #             i, item.title, item.meta['author'].name).strip()
        #     else:
        #         nextline = "`{}.` **{}**".format(i, item.title).strip()

        #     counter += 1

        # message = '\n'.join(lines)
        # return Response(message, delete_after=30)

        # TODO implement cmd_history and required infrastructure.

    async def cmd_queue(self, channel, player):
        """
        Usage:
            {command_prefix}queue

        Prints the queued songs.
        """

        lines = []
        unlisted = 0
        moretext = "* ... and %s more*" % ('x' * len(player.playlist.entries))

        if player.current_entry:
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(
                timedelta(seconds=player.current_entry.duration))
            prog_str = "`[%s/%s]`" % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and \
                    player.current_entry.meta.get('author', False):
                lines.append(
                    "Currently Playing: **%s** added by **%s** %s\n" % (
                        player.current_entry.title,
                        player.current_entry.meta['author'].name,
                        prog_str))
            else:
                lines.append("Now Playing: **%s** %s\n" %
                             (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and \
                    item.meta.get('author', False):
                nextline = "`{}.` **{}** added by **{}**".format(
                    i, item.title, item.meta['author'].name).strip()
            else:
                nextline = "`{}.` **{}**".format(i, item.title).strip()

            # +1 is for newline char
            currentlinesum = sum(len(x) + 1 for x in lines)

            if currentlinesum + len(nextline) + len(moretext) \
                    > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(moretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append("\n*... and %s more*" % unlisted)

        if not lines:
            lines.append(
                "There are no songs queued! Queue something with {}play."
                .format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self,
                        message,
                        channel,
                        server,
                        author,
                        search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat.
        Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("Enter a number.  NUMBER.  That means digits.  \
            `15`.  Etc.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in
                [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(
            author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(
                    channel, check=check, limit=search_range, before=message)
                return Response(
                    "Cleaned up {} message{}."
                    .format(len(deleted),
                            's' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response("Cleaned up {} message{}."
                        .format(deleted, 's' * bool(deleted)), delete_after=6)

    async def cmd_pldump(self, channel, song_url):
        """
        Usage:
            {command_prefix}pldump url

        Dumps the individual urls of a playlist
        """

        try:
            info = await self.downloader.extract_info(
                self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(
                "Could not extract info from input url\n%s\n" % e,
                expire_in=25)

        if not info:
            raise exceptions.CommandError(
                "Could not extract info from input url, no data.",
                expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get(
                    'webpage_url', info.get('url', None)):
                raise exceptions.CommandError(
                    "This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube": lambda d:
                       'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp": lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError(
                "Could not extract info from input url, \
                unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await self.send_file(
                channel, fcontent, filename='playlist.txt',
                content="Here's the url dump for <%s>" % song_url)

        return Response("\n:mailbox_with_mail: ", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels',
                'roles',
                'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + " ".join(["`%s`" % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ["Your ID: %s" % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ["%s #%s: %s" % (
                    m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ["%s: %s" % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type ==
                          discord.ChannelType.text]
                rawudata = ["%s: %s" % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type ==
                          discord.ChannelType.voice]
                rawudata.extend("%s: %s" % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename="%s-ids-%s.txt" %
                                 (server.name.replace(' ', '_'), cat))

        return Response("\n:mailbox_with_mail: ", delete_after=20)

    async def cmd_perms(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}perms

        Sends the user a list of their permissions.
        """

        lines = ["Command permissions in %s\n" % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" %
                         (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response("\n:mailbox_with_mail: ", delete_after=20)


    async def cmd_repeat(self, player):
        """
        Usage:
            {command_prefix}repeat

        Cycles through the repeat options.
        Default is no repeat, switchable to repeat all or repeat current song.
        """

        if player.is_stopped:
            raise exceptions.CommandError(
                "Can't change repeat mode! The player is not playing!",
                expire_in=20)

        player.repeat()

        if player.is_repeat_none:
            return Response(":play_pause: Repeat mode: None", delete_after=20)
        if player.is_repeat_all:
            return Response(":repeat: Repeat mode: All", delete_after=20)
        if player.is_repeat_single:
            return Response(":repeat_one: Repeat mode: Single", delete_after=20)


    async def cmd_promote(self, player, position=None):
        """
        Usage:
            {command_prefix}promote
            {command_prefix}promote [song position]

        Promotes the last song in the queue to the front.
        If a position is specifyed, the song is promotes to be first in the queue.
        """

        if player.is_stopped:
            raise exceptions.CommandError(
                'Can not modify the queue! The player is not playing!',
                expire_in=20)

        length = len(player.playlist.entries)

        if length < 2:
            raise exceptions.CommandError(
                'Can not promote! Please add at least 2 songs to the queue!',
                expire_in=20)

        if not position:
            entry = await player.playlist.promote_entry()
        else:
            try:
                position = int(position)
            except ValueError:
                raise exceptions.CommandError(
                    "This is not a valid song number! Please choose a song \
                    number between 2 and %s!" % length,
                    expire_in=20)

            if position == 1:
                raise exceptions.CommandError(
                    "This song is already at the top of the queue!",
                    expire_in=20)
            if position < 1 or position > length:
                raise exceptions.CommandError(
                    "Can't promote a song not in the queue! Please choose a song \
                    number between 2 and %s!" % length,
                    expire_in=20)

            entry = await player.playlist.promote_entry(position)


        reply_text = """Promoted **%s** to the :top: of the queue.\n
        Estimated time until playing: %s"""

        try:
            time_until = await player.playlist.estimate_time_until(1, player)
        except:
            traceback.print_exc()
            time_until = ''

        reply_text %= (entry.title, time_until)

        return Response(reply_text, delete_after=30)


    async def cmd_remove(self, player, position=None):
        """
        Usage:
            {command_prefix}remove
            {command_prefix}remove [song position]

        Removes the next song from the queue.
        If you specify a position, it removes the song at that queue position.
        """

        if player.is_stopped:
            raise exceptions.CommandError(
                "Can't modify the queue! The player is not playing!",
                expire_in=20)

        length = len(player.playlist.entries)

        if length < 1:
            raise exceptions.CommandError(
                "Can't remove! Please add at least 1 song to the queue!",
                expire_in=20)

        if not position:
            entry = player.playlist.remove_entry()
        else:
            try:
                position = int(position)
            except ValueError:
                raise exceptions.CommandError(
                    "This is not a valid song number! Please choose a song \
                    number between 1 and %s!" % length,
                    expire_in=20)

            if position == 1:
                entry = player.playlist.remove_entry(position)
            elif position < 1 or position > length:
                raise exceptions.CommandError(
                    "Can't remove a song not in the queue! Please choose a song \
                    number between 1 and %s!" % length,
                    expire_in=20)
            else:
                entry = await player.playlist.remove_entry(position)

        reply_text = ":x: Removed **%s** from the queue." % entry.title

        return Response(reply_text, delete_after=30)


    async def cmd_playnow(self,
                          player,
                          channel,
                          author,
                          permissions,
                          leftover_args,
                          song_url):
        """
        Usage:
            {command_prefix}playnow song_link
            {command_prefix}playnow text to search for

        Immediately stops playing and starts plays the requested song. \
        If a link is not provided, the 1st result from a youtube search is played.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) \
                >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" %
                permissions.max_songs,
                expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        try:
            info = await self.downloader.extract_info(
                player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError(
                "That video cannot be played.", expire_in=30)

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # LOG.info("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e,
                                           expire_in=120),
                    loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    """Error extracting info from search string,
                    youtubedl returned no data.\n
                    You may need to restart the bot if this continues to happen.""",
                    expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                return

            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(
                player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, \
            # author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like
        # the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing,
        # but finds two different urls

        if 'entries' in info:
            raise exceptions.CommandError(
                "Cannot playnow playlists! You must specify a single song.",
                expire_in=30)
        else:
            if permissions.max_song_length and info.get('duration', 0) > \
                    permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (
                        info['duration'],
                        permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(
                    song_url, channel=channel, author=author)
                await self.safe_send_message(
                    channel,
                    """Enqueued **%s** to be played.
                Position in queue: Up next!""" % entry.title,
                    expire_in=20)
                # Get the song ready now, otherwise race condition where
                # finished-playing will fire before the song is finished
                # downloading, which will then cause another song from
                # autoplaylist to be added to the queue
                await entry.get_ready_future()

            except exceptions.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    LOG.warning(
                        """Determined incorrect entry type, but suggested url
                        is the same. Help.""")

                if self.config.debug_mode:
                    LOG.info(
                        """Assumed url `%s` was a single entry, was actually a
                        playlist""" % song_url)
                    LOG.info("Using `%s` instead" % e.use_url)

                return await self.cmd_playnow(
                    player, channel, author, permissions, leftover_args, e.use_url)

            if position > 1:
                player.playlist.promote_entry()
            if player.is_playing:
                player.skip()
            # return Response(reply_text, delete_after=30)


    async def cmd_roll(self, channel, author, leftover_args):
        """
        Usage:
            {command_prefix}roll [1-100]d[MAXROLL]
            {command_prefix}roll [MAXROLL]

        Roll the set number of dice and show the sum of the rolls, or
        roll one die and show the result.
        """
        if not leftover_args:
            raise exceptions.CommandError("""Unable to roll dice.\n
            Usage: {command_prefix}roll [NUMDICE]d[4-20] or \n{command_prefix}roll [MAXROLL].""",
                                          expire_in=20)

        diceInput = ''.join(leftover_args)
        res = re.search(r"(^\d+d\d+$)|(^\d+$)", diceInput)
        if not res:
            raise exceptions.CommandError("Unable to roll dice.\n"
            "Usage: {command_prefix}roll [NUMDICE]d[4-20] or \n{command_prefix}roll [MAXROLL].",
                                          expire_in=20)
        match = res.group(0)
        diceVals = match.split('d')
        if len(diceVals) == 2:
            numDice = int(diceVals[0])
            maxRoll = int(diceVals[1])
            if not 1 <= numDice <= 100:
                raise exceptions.CommandError(
                    "Unable to roll dice.\n"
                    "Usage: {command_prefix}roll [1-100]d[MAXROLL].",
                    expire_in=20)
            if maxRoll < 1:
                raise exceptions.CommandError(
                    "Unable to roll dice. Maximum dice value must be at least 1.",
                    expire_in=20)
            rollSum = 0
            for i in range(0, numDice):
                rollSum += random.randint(1, maxRoll)
            return Response(
                ":game_die: %s used %s to roll a %d." %
                (author.mention, diceInput, rollSum),
                delete_after=30)
        else:
            maxRoll = int(diceVals[0])
            if maxRoll < 1:
                raise exceptions.CommandError(
                    "Unable to roll dice. Maximum dice value must be at least 1.",
                    expire_in=20)
            roll = random.randint(1, maxRoll)
            return Response(
                ":game_die: %s rolled a %d." % (author.mention, roll),
                delete_after=30)
        return

    @owner_only
    async def cmd_reloadperm(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}reloadperm

        reloads the permissions file
        """
        try:
            perms_file = PermissionsDefaults.perms_file
            self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
            await self.send_message(author, ":ok_hand:")
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)
        return

    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "Failed to change name. Did you change names too many times? "
                "Remember name changes are limited to twice per hour.", expire_in=20)

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\n:ok_hand:", delete_after=20)

    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError(
                "Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\n:ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(self.timeout):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as error:
            raise exceptions.CommandError(
                "Unable to change avatar: %", error, expire_in=20)

        return Response("\n:ok_hand:", delete_after=20)

    async def cmd_disconnect(self, server):
        await self.disconnect_voice_client(server)
        return Response("\n:dash:", delete_after=20)

    async def cmd_restart(self, channel):
        await self.safe_send_message(channel, "\n:wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()

    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, "\n:wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal()

    @dev_only
    async def cmd_breakpoint(self, message):
        LOG.critical("Activating debug breakpoint")
        return

    @dev_only
    async def cmd_objgraph(self, channel, func='most_common_types()'):
        import objgraph

        await self.send_typing(channel)

        if func == 'growth':
            f = StringIO()
            objgraph.show_growth(limit=10, file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leaks':
            f = StringIO()
            objgraph.show_most_common_types(
                objects=objgraph.get_leaking_objects(), file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leakstats':
            data = objgraph.typestats(objects=objgraph.get_leaking_objects())

        else:
            data = eval('objgraph.' + func)

        return Response(data, codeblock='py')

    @dev_only
    async def cmd_debug(self, message, _player, *, data):
        # player = _player
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        try:
            result = eval(code)
        except:
            try:
                exec(code)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            LOG.warning(
                "Ignoring command from myself (%s)", message.content)
            return

        if self.config.bound_channels and message.channel.id not in \
                self.config.bound_channels and not message.channel.is_private:
            return
            # if I want to log this I just move it under the prefix check

        # Uh, doesn't this break prefixes with spaces in them (it doesn't,
        # config parser already breaks them)
        command, *args = message_content.split(' ')
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            return

        # TODO private_msg_list should be added to permissions <---> check out user_permissions.command_whitelist
        private_msg_list = ['joinserver', 'ban', 'reloadperm', 'setavatar', 'restart', 'help']
        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and
                    command in private_msg_list):
                await self.send_message(
                    message.channel,
                    'You cannot use this bot in private messages.')
                return

        if message.author.id in self.banned and \
                message.author.id != self.config.owner_id:
            LOG.warning(
                "User banned: {0.id}/{0!s} ({1})"
                .format(message.author, command))
            return

        else:
            LOG.info("{0.id}/{0!s}: {1}".format(message.author, message_content
                                                .replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in \
                    user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(
                    message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.server)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(
                    map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(
                    map(message.server.get_channel,
                        message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = \
                    message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and \
                        param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = "[{}={}]".format(
                    key, param.default) if param.default is not \
                    param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had
                # no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in\
                        user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({})."
                        .format(user_permissions.name), expire_in=20)

                elif user_permissions.command_blacklist and command in \
                        user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({})."
                        .format(user_permissions.name),
                        expire_in=20)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}' \
                        .format(
                            self.config.command_prefix,
                            command,
                            ' '.join(args_expected)
                        )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'
                    .format(
                        docs.format(
                            command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '{}, {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if
                    self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking
                    else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError,
                exceptions.ExtractionError) as e:
            LOG.error("Error in {0}: {1.__class__.__name__}: {1.message}"
                      .format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n{}\n```'.format(e.message),
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            LOG.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(
                    message.channel, '```\n%s\n```',
                    traceback.format_exc())

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)

    async def on_voice_state_update(self, before, after):
        if not self.init_ok:
            return  # Ignore stuff before ready

        state = VoiceStateUpdate(before, after)

        if state.broken:
            LOG.voicedebug("Broken voice state update")
            return

        if state.resuming:
            LOG.debug(
                "Resumed voice connection to {0.server.name}/{0.name}"
                .format(state.voice_channel))

        if not state.changes:
            LOG.voicedebug(
                "Empty voice state update, likely a session id change")
            return  # Session id change, pointless event

        ################################

        LOG.voicedebug("Voice state update for {mem.id}/{mem!s} on {ser.name}/{vch.name} -> {dif}"
                       .format
                       (
                           mem=state.member,
                           ser=state.server,
                           vch=state.voice_channel,
                           dif=state.changes
                       ))

        if not state.is_about_my_voice_channel:
            return  # Irrelevant channel

        if state.joining or state.leaving:
            LOG.info("{0.id}/{0!s} has {1} {2}/{3}".format(
                state.member,
                'joined' if state.joining else 'left',
                state.server,
                state.my_voice_channel
            ))

        if not self.config.auto_pause:
            return

        autopause_msg = \
            "{state} in {channel.server.name}/{channel.name} {reason}"

        auto_paused = self.server_data[after.server]['auto_paused']
        player = await self.get_player(state.my_voice_channel)

        if state.joining and state.empty() and player.is_playing:
            LOG.info(autopause_msg.format(
                state="Pausing",
                channel=state.my_voice_channel,
                reason="(joining empty channel)"
            ).strip())

            self.server_data[after.server]['auto_paused'] = True
            player.pause()
            return

        if not state.is_about_me:
            if not state.empty(old_channel=state.leaving):
                if auto_paused and player.is_paused:
                    LOG.info(autopause_msg.format(
                        state="Unpausing",
                        channel=state.my_voice_channel,
                        reason=""
                    ).strip())

                    self.server_data[after.server]['auto_paused'] = False
                    player.resume()
            else:
                if not auto_paused and player.is_playing:
                    LOG.info(autopause_msg.format(
                        state="Pausing",
                        channel=state.my_voice_channel,
                        reason="(empty channel)"
                    ).strip())

                    self.server_data[after.server]['auto_paused'] = True
                    player.pause()

    async def on_server_update(self,
                               before: discord.Server,
                               after: discord.Server):
        if before.region != after.region:
            LOG.warning("Server \"%s\" changed regions: %s -> %s" %
                        (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)

    async def on_server_join(self, server: discord.Server):
        LOG.info("Bot has been joined server: %s", server.name)

        if not self.user.bot:
            alertmsg = "<@{uid}> Hi I'm a musicbot please mute me."

            # Discord API
            if server.id == "81384788765712384" and not server.unavailable:
                playground = server.get_channel("94831883505905664") or \
                    discord.utils.get(
                        server.channels, name='playground') or server
                # fake abal
                await self.safe_send_message(
                    playground, alertmsg.format(uid="98295630480314368"))

            # Rhino Bot Help
            elif server.id == "129489631539494912" and not server.unavailable:
                bot_testing = server.get_channel("134771894292316160") or \
                    discord.utils.get(
                        server.channels, name='bot-testing') or server
                # also fake abal
                await self.safe_send_message(
                    bot_testing, alertmsg.format(uid="98295630480314368"))

        LOG.debug("Creating data folder for server %s", server.id)
        pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)

    async def on_server_remove(self, server: discord.Server):
        LOG.info("Bot has been removed from server: %s", server.name)
        LOG.debug('Updated server list:')
        [LOG.debug(' - ' + s.name) for s in self.servers]

        if server.id in self.players:
            self.players.pop(server.id).kill()

    async def on_server_available(self, server: discord.Server):
        if not self.init_ok:
            return  # Ignore pre-ready events

        LOG.debug("Server \"%s\" has become available.", server.name)

        player = self.get_player_in(server)

        if player and player.is_paused:
            av_paused = \
                self.server_data[server]['availability_paused']

            if av_paused:
                LOG.debug(
                    "Resuming player in \"%s\" due to availability.",
                    server.name)
                self.server_data[server]['availability_paused'] = False
                player.resume()

    async def on_server_unavailable(self, server: discord.Server):
        LOG.debug("Server \"%s\" has become unavailable.". server.name)

        player = self.get_player_in(server)

        if player and player.is_playing:
            LOG.debug(
                "Pausing player in \"%s\" due to unavailability.",
                server.name)
            self.server_data[server]['availability_paused'] = True
            player.pause()
