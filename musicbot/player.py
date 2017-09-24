import os
import sys
import json
import logging
import asyncio
import subprocess

from enum import Enum
from array import array
from threading import Thread
from collections import deque
from shutil import get_terminal_size

import audioop

from websockets.exceptions import InvalidState

from .utils import avg, format_time_ffmpeg
from .lib.event_emitter import EventEmitter
from .constructs import Serializable, Serializer
from .entry import URLPlaylistEntry
from .exceptions import FFmpegError, FFmpegWarning

LOG = logging.getLogger(__name__)


class PatchedBuff:
    """
        PatchedBuff monkey patches a readable object,
         allowing you to vary what the volume is as the song is playing.
    """

    def __init__(self, buff, *, draw=False):
        self.buff = buff
        self.frame_count = 0
        self.volume = 1.0

        self.draw = draw
        self.use_audioop = True
        self.frame_skip = 2
        self.rmss = deque([2048], maxlen=90)

    def __del__(self):
        if self.draw:
            print(' ' * (get_terminal_size().columns - 1), end='\r')

    def read(self, frame_size):
        """ TODO """
        self.frame_count += 1

        frame = self.buff.read(frame_size)

        if self.volume != 1:
            frame = self._frame_vol(frame, self.volume, maxv=10)

        if self.draw and not self.frame_count % self.frame_skip:
            # these should be processed for every frame, but "overhead"
            rms = audioop.rms(frame, 2)
            self.rmss.append(rms)

            max_rms = sorted(self.rmss)[-1]
            meter_text = 'avg rms: {:.2f}, max rms: {:.2f} ' \
                .format(avg(self.rmss), max_rms)
            self._pprint_meter(rms / max(1, max_rms),
                               text=meter_text, shift=True)

        return frame

    def _frame_vol(self, frame, mult, *, maxv=10, use_audioop=True):
        if use_audioop:
            return audioop.mul(frame, 2, min(mult, maxv))
        else:
            # ffmpeg returns s16le pcm frames.
            frame_array = array('h', frame)

            for i in enumerate(frame_array):
                frame_array[i] = int(frame_array[i] * min(mult, min(1, maxv)))

            return frame_array.tobytes()

    def _pprint_meter(self, perc, *, char='#', text='', shift=True):
        terminal_size = get_terminal_size()

        if shift:
            outstr = text + \
                "{}".format(
                    char * (int((terminal_size - len(text)) * perc) - 1))
        else:
            outstr = text + \
                "{}".format(char * (int(terminal_size * perc) - 1))[len(text):]

        print(outstr.ljust(terminal_size - 1), end='\r')


class PlayerState(Enum):
    """ TODO """
    STOPPED = 0  # When the player isn't playing anything
    PLAYING = 1  # Player is actively playing music
    PAUSED = 2   # Player is paused on a song
    WAITING = 3  # Player has finished song but is downloading the next one
    DEAD = 4     # Player has been killed

    def __str__(self):
        return self.name


class PlayerRepeatState(Enum):
    """ TODO """
    NONE = 0    # Playlist plays as normal
    ALL = 1     # Entire playlist repeats
    SINGLE = 2  # Currently playing song repeats

    def __str__(self):
        return self.name


class Player(EventEmitter, Serializable):
    """ A play manages the FFMPEG player instance and all that is associated with it. """

    def __init__(self, bot, voice_client, playlist):
        super().__init__()
        self.bot = bot
        self.loop = bot.loop
        self.voice_client = voice_client
        self.playlist = playlist
        self.deliberately_killed = False
        self.state = PlayerState.STOPPED
        self.skip_state = None
        self._volume = bot.config.default_volume
        self.repeat_state = PlayerRepeatState.NONE
        self.skip_repeat = False
        self._play_lock = asyncio.Lock()
        self._current_player = None
        self._current_entry = None
        self._previous_entry = None
        self._stderr_future = None
        self.playlist.on('entry-added', self.on_entry_added)
        self.playlist.on('entry-removed', self.on_entry_removed)
        self.loop.create_task(self.websocket_check())

    @property
    def volume(self):
        """ TODO """
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = value
        if self._current_player:
            self._current_player.buff.volume = value

    def on_entry_added(self, playlist, entry):
        """ TODO """
        if self.is_stopped:
            self.loop.call_later(2, self.play)

        self.emit('entry-added', player=self, playlist=playlist, entry=entry)

    def on_entry_removed(self, playlist, entry):
        """ TODO """
        if not self.bot.config.save_videos and entry:
            if any([entry.filename == e.filename for e in
                    self.playlist.entries]):
                LOG.debug("""[Config:SaveVideos] Skipping deletion,
                    found song in queue""")
            elif entry.filename == self._current_entry.filename:
                LOG.debug(
                    """[Config:SaveVideos] Skipping deletion,
                    song removed from queue is currently playing""")
            else:
                # LOG.debug("[Config:SaveVideos] Deleting file: %s" % \
                # os.path.relpath(entry.filename))
                asyncio.ensure_future(self._delete_file(entry.filename))
                # TODO USE EMIT FOR remove
                # self.emit('entry-remove', player=self, playlist=playlist, entry=entry)

    def skip(self):
        """ TODO """
        if self.is_repeat_single:
            self.skip_repeat = True
        self._kill_current_player()

    def goto_seconds(self, secs):
        """ TODO """
        if (not self.current_entry) or secs >= self.current_entry.duration:
            return False

        c_entry = self.current_entry
        c_entry.set_start(secs)
        self.play_entry(c_entry)
        return True

    def stop(self):
        """ TODO """
        self.state = PlayerState.STOPPED
        self._kill_current_player()

        self.emit('stop', player=self)

    def resume(self):
        """ TODO """
        if self.is_paused and self._current_player:
            self._current_player.resume()
            self.state = PlayerState.PLAYING
            self.emit('resume', player=self, entry=self.current_entry)
            return

        if self.is_paused and not self._current_player:
            self.state = PlayerState.PLAYING
            self._kill_current_player()
            return

        raise ValueError("Cannot resume playback from state %s" % self.state)

    def pause(self):
        """ TODO """
        if self.is_playing:
            self.state = PlayerState.PAUSED

            if self._current_player:
                self._current_player.pause()

            self.emit('pause', player=self, entry=self.current_entry)
            return

        elif self.is_paused:
            return

        raise ValueError("Cannot pause a Player in state %s" % self.state)

    def repeat(self):
        """ TODO """
        if self.is_repeat_none:
            self.repeat_state = PlayerRepeatState.ALL
            return
        if self.is_repeat_all:
            self.repeat_state = PlayerRepeatState.SINGLE
            return
        if self.is_repeat_single:
            self.repeat_state = PlayerRepeatState.NONE
            return

    def kill(self):
        """ TODO """
        self.state = PlayerState.DEAD
        self.playlist.clear()
        self._events.clear()
        self._kill_current_player()

    def _playback_finished(self):
        entry = self._current_entry
        self._previous_entry = entry
        self._current_entry = None
        self._current_player = None

        if self.deliberately_killed:
            self.deliberately_killed = False
            return

        if self.is_repeat_all or (self.is_repeat_single and not self.skip_repeat):
            self.playlist._add_entry(entry)
            if self.is_repeat_single:
                self.playlist.promote_entry()
        self.skip_repeat = False

        if self._current_player:
            self._current_player.after = None
            self._kill_current_player()

        self._current_entry = None

        if self._stderr_future.done() and self._stderr_future.exception():
            # I'm not sure that this would ever not be done if it gets to this
            # point unless ffmpeg is doing something highly questionable
            self.emit('error', player=self, entry=entry,
                      ex=self._stderr_future.exception())

        if not self.is_stopped and not self.is_dead:
            self.play(_continue=True)

        if not self.bot.config.save_videos and entry:
            if any([entry.filename == e.filename for e in
                    self.playlist.entries]):
                LOG.debug(
                    "Skipping deletion of \"%s\", found song in queue",
                    entry.filename)
            elif self.bot.config.show_thumbnails:
                LOG.debug("Deleting song & thumbnail file: \n%s\n%s",
                          os.path.relpath(entry.filename),
                          os.path.relpath(entry.filename_thumbnail)
                         )
                asyncio.ensure_future(self._delete_file(entry.filename, entry.filename_thumbnail))
            else:
                LOG.debug("Deleting song file: %s", os.path.relpath(entry.filename))
                asyncio.ensure_future(self._delete_file(entry.filename))

        self.emit('finished-playing', player=self, entry=entry)

    def _kill_current_player(self):
        if self._current_player:
            if self.is_paused:
                self.resume()

            try:
                self._current_player.stop()
            except OSError:
                pass

            self._previous_entry = self._current_entry
            self._current_player = None

            return True
        else:
            return False

    async def _delete_file(self, filename, thumbnail=None):
        """ TODO """
        for x in range(30):
            try:
                os.unlink(filename)
                if thumbnail is not None:
                    os.unlink(thumbnail)
                break

            except PermissionError as error:
                if error.winerror == 32:  # File is in use
                    await asyncio.sleep(0.25)

            except Exception:
                LOG.error("Error trying to delete %s", filename, exc_info=True)
                break
        else:
            LOG.debug("""[Config:SaveVideos] Could not delete file %s,
            giving up and moving on""", os.path.relpath(filename))

    def play(self, _continue=False):
        """ TODO """
        self.loop.create_task(self._play(_continue=_continue))

    async def _play(self, _continue=False):
        """
            Plays the next entry from the playlist,
             or resumes playback of the current entry if paused.
        """
        if self.is_paused:
            return self.resume()

        if self.is_dead:
            return

        with await self._play_lock:
            if self.is_stopped or _continue:
                try:
                    entry = await self.playlist.get_next_entry()

                except:
                    LOG.warning("Failed to get entry, retrying", exc_info=True)
                    self.loop.call_later(0.1, self.play)
                    return

                # If nothing left to play, transition to the stopped state.
                if not entry:
                    self.stop()
                    return

                # In-case there was a player, kill it. RIP.
                self._kill_current_player()

                boptions = "-nostdin"
                # aoptions = "-vn -b:a 192k"
                aoptions = "-vn"

                LOG.ffmpeg("Creating player with options: %s %s %s" % (
                    boptions, aoptions, entry.filename))

                self._current_player = self._monkeypatch_player(
                    self.voice_client.create_ffmpeg_player(
                        entry.filename,
                        before_options=boptions,
                        options=aoptions,
                        stderr=subprocess.PIPE,
                        # Threadsafe call soon, b/c after will be
                        # called from the voice playback thread.
                        after=lambda: self.loop.call_soon_threadsafe(
                            self._playback_finished)
                    ))
                self._current_player.setDaemon(True)
                self._current_player.buff.volume = self.volume

                # TODO I need to add ytdl hooks
                self.state = PlayerState.PLAYING
                self._current_entry = entry
                self._stderr_future = asyncio.Future()

                stderr_thread = Thread(
                    target=filter_stderr,
                    args=(self._current_player.process, self._stderr_future),
                    name="{} stderr reader".format(self._current_player.name)
                )

                stderr_thread.start()
                self._current_player.start()

                self.emit('play', player=self, entry=entry)

    def play_entry(self, entry):
        """ TODO """
        self.loop.create_task(self._play_entry(entry))

    async def _play_entry(self, entry):
        """
            Plays the provided entry.
        """

        with await self._play_lock:
            # In-case there was a player, kill it. RIP.
            self.deliberately_killed = True
            self._kill_current_player()

            self._current_player = self._monkeypatch_player(self.voice_client.create_ffmpeg_player(
                entry.filename,
                before_options="-nostdin -ss {}".format(
                    format_time_ffmpeg(int(entry.start_seconds))),
                # options="-vn -b:a 128k",
                options="-vn",
                # Threadsafe call soon, b/c after will be called from the voice
                # playback thread.
                after=lambda: self.loop.call_soon_threadsafe(
                    self._playback_finished)
            ))
            self._current_player.setDaemon(True)
            self._current_player.buff.volume = self.volume

            # TODO I need to add ytdl hooks
            self.state = PlayerState.PLAYING
            self._current_entry = entry

            self._current_player.start()
            self.emit('play', player=self, entry=entry)

    def _monkeypatch_player(self, player):
        original_buff = player.buff
        player.buff = PatchedBuff(original_buff)
        return player

    async def reload_voice(self, voice_client):
        """ TODO """
        async with self.bot.aiolocks[self.reload_voice.__name__ + ':'
                                     + voice_client.channel.server.id]:
            self.voice_client = voice_client
            if self._current_player:
                self._current_player.player = voice_client.play_audio
                self._current_player._resumed.clear()
                self._current_player._connected.set()

    async def websocket_check(self):
        """ TODO """
        LOG.voicedebug("Starting websocket check loop for %s",
                       self.voice_client.channel.server)

        while not self.is_dead:
            try:
                async with self.bot.aiolocks[
                    self.reload_voice.__name__ + ':'
                    + self.voice_client.channel.server.id]:
                    await self.voice_client.ws.ensure_open()

            except InvalidState:
                LOG.debug("Voice websocket for \"%s\" is %s, reconnecting",
                          self.voice_client.channel.server, self.voice_client.ws.state_name)
                await self.bot.reconnect_voice_client(
                    self.voice_client.channel.server,
                    channel=self.voice_client.channel
                )
                await asyncio.sleep(3)

            except Exception:
                LOG.error("Error in websocket check loop", exc_info=True)

            finally:
                await asyncio.sleep(1)

    def __json__(self):
        return self._enclose_json({
            'current_entry': {
                'entry': self.current_entry,
                'progress': self.progress,
                'progress_frames': self._current_player.buff.frame_count if
                                   self.progress is not None else None
            },
            'entries': self.playlist
        })

    @classmethod
    def _deserialize(cls, data, bot=None, voice_client=None, playlist=None):
        """ TODO """
        assert bot is not None, cls._bad('bot')
        assert voice_client is not None, cls._bad('voice_client')
        assert playlist is not None, cls._bad('playlist')

        player = cls(bot, voice_client, playlist)

        data_pl = data.get('entries')
        if data_pl and data_pl.entries:
            player.playlist.entries = data_pl.entries

        current_entry_data = data['current_entry']
        if current_entry_data['entry']:
            player.playlist.entries.appendleft(current_entry_data['entry'])
            # TODO: progress stuff
            # how do I even do this
            # this would have to be in the entry class right?
            # some sort of progress indicator to skip ahead with ffmpeg
            # (however that works, reading and ignoring frames?)

        return player

    @classmethod
    def from_json(cls, raw_json, bot, voice_client, playlist):
        """ TODO """
        try:
            return json.loads(raw_json, object_hook=Serializer.deserialize)
        except:
            LOG.exception("Failed to deserialize player")

    @property
    def current_entry(self):
        """ TODO """
        return self._current_entry

    @property
    def previous_entry(self):
        """ TODO """
        return self._previous_entry

    @property
    def is_playing(self):
        """ TODO """
        return self.state == PlayerState.PLAYING

    @property
    def is_paused(self):
        """ TODO """
        return self.state == PlayerState.PAUSED

    @property
    def is_stopped(self):
        """ TODO """
        return self.state == PlayerState.STOPPED

    @property
    def is_dead(self):
        """ TODO """
        return self.state == PlayerState.DEAD

    @property
    def is_repeat_none(self):
        """ TODO """
        return self.repeat_state == PlayerRepeatState.NONE

    @property
    def is_repeat_all(self):
        """ TODO """
        return self.repeat_state == PlayerRepeatState.ALL

    @property
    def is_repeat_single(self):
        """ TODO """
        return self.repeat_state == PlayerRepeatState.SINGLE

    @property
    def progress(self):
        """ TODO """
        if self._current_player:
            if isinstance(self._current_entry, URLPlaylistEntry):
                 ret = round(self._current_player.buff.frame_count * 0.02) + \
                (self.current_entry.start_seconds if self.current_entry is not None else 0)
            else:
                ret = 0
        
            return ret
            # TODO: Properly implement this
            #       Correct calculation should be bytes_read/192k
            #       192k AKA sampleRate * (bitDepth / 8) * channelCount
            #       Change frame_count to bytes_read in the PatchedBuff

# TODO: I need to add a check for if the eventloop is closed


def filter_stderr(popen: subprocess.Popen, future: asyncio.Future):
    """ TODO """
    last_ex = None

    while True:
        data = popen.stderr.readline()
        if data:
            LOG.ffmpeg("Data from ffmpeg: %s", data)
            try:
                if check_stderr(data):
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()

            except FFmpegError as error:
                LOG.ffmpeg("Error from ffmpeg: %s", str(error).strip())
                last_ex = error

            except FFmpegWarning:
                pass  # useless message
        else:
            break

    if last_ex:
        future.set_exception(last_ex)
    else:
        future.set_result(True)


def check_stderr(data: bytes):
    """ TODO """
    try:
        data = data.decode('utf8')
    except:
        LOG.ffmpeg("Unknown error decoding message from ffmpeg", exc_info=True)
        return True  # fuck it

    # LOG.ffmpeg("Decoded data from ffmpeg: %s", data)

    # TODO: Regex
    warnings = [
        "Header missing",
        "Estimating duration from birate, this may be inaccurate",
        "Using AVStream.codec to pass codec parameters to muxers is "
        "deprecated, use AVStream.codecpar instead.",
        "Application provided invalid, non monotonically increasing dts to "
        "muxer in stream",
        "Last message repeated",
        "Failed to send close message",
        "decode_band_types: Input buffer exhausted before END element found"
    ]
    errors = [
        # need to regex this properly, its both a warning and an error
        "Invalid data found when processing input",
    ]

    if any(msg in data for msg in warnings):
        raise FFmpegWarning(data)

    if any(msg in data for msg in errors):
        raise FFmpegError(data)

    return True


# if redistributing ffmpeg is an issue, it can be downloaded from here:
# - http://ffmpeg.zeranoe.com/builds/win32/static/ffmpeg-latest-win32-static.7z
# - http://ffmpeg.zeranoe.com/builds/win64/static/ffmpeg-latest-win64-static.7z
#
# Extracting bin/ffmpeg.exe, bin/ffplay.exe, and bin/ffprobe.exe should be fine
# However, the files are in 7z format so meh
# I don't know if we can even do this for the user,
# at most we open it in the browser. I can't imagine the user is so incompetent
# that they can't pull 3 files out of it...
# ...
# ...right?

# Get duration with ffprobe
#   ffprobe.exe -v error -show_entries format=duration \
# -of default=noprint_wrappers=1:nokey=1 -sexagesimal filename.mp3
# This is also how I fix the format checking issue for now
# ffprobe -v quiet -print_format json -show_format stream

# Normalization filter
# -af dynaudnorm
