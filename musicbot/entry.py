import os
import re
import asyncio
import logging
import traceback

from enum import Enum
from .constructs import Serializable
from .exceptions import ExtractionError
from .utils import get_header, md5sum

LOG = logging.getLogger(__name__)


class EntryTypes(Enum):
    """ TODO """
    URL = 1
    STEAM = 2
    FILE = 3

    def __str__(self):
        return self.name


class BasePlaylistEntry(Serializable):
    """ TODO """

    def __init__(self):
        self.filename = None
        self.filename_thumbnail = None
        self._is_downloading = False
        self._waiting_futures = []

    @property
    def is_downloaded(self):
        """ TODO """
        if self._is_downloading:
            return False

        return bool(self.filename)

    async def _download(self):
        raise NotImplementedError

    def get_ready_future(self):
        """
            Returns a future that will fire when the song is ready to be played.
            The future will either fire with the result (being the entry)
            or an exception as to why the song download failed.
        """
        future = asyncio.Future()
        if self.is_downloaded:
            # In the event that we're downloaded, we're already ready for
            # playback.
            future.set_result(self)

        else:
            # If we request a ready future, let's ensure that it'll actually
            # resolve at one point.
            asyncio.ensure_future(self._download())
            self._waiting_futures.append(future)

        return future

    def _for_each_future(self, callback):
        """
            Calls `callback` for each future that is not cancelled.
            Absorbs and logs any errors that may have occurred.
        """
        futures = self._waiting_futures
        self._waiting_futures = []

        for future in futures:
            if future.cancelled():
                continue

            try:
                callback(future)

            except:
                traceback.print_exc()

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


class URLPlaylistEntry(BasePlaylistEntry):
    """ TODO """

    def __init__(self,
                 playlist,
                 url,
                 title,
                 duration=0,
                 start_seconds=0,
                 expected_filename=None,
                 filename_thumbnail=None,
                 **meta):
        super().__init__()

        self.playlist = playlist
        self.url = url
        self.title = title
        self.duration = duration
        self.start_seconds = start_seconds
        self.expected_filename = expected_filename
        self.filename_thumbnail = filename_thumbnail
        self.meta = meta
        self.download_folder = self.playlist.downloader.download_folder

    def __json__(self):
        return self._enclose_json({
            'version': 2,
            'url': self.url,
            'title': self.title,
            'duration': self.duration,
            'start_seconds': self.start_seconds,
            'downloaded': self.is_downloaded,
            'expected_filename': self.expected_filename,
            'filename': self.filename,
            'filename_thumbnail': self.filename_thumbnail,
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for name, obj in self.meta.items() if obj
            }
        })

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            duration = data['duration']
            start_seconds = data['start_seconds']
            downloaded = data['downloaded']
            filename = data['filename'] if downloaded else None
            expected_filename = data['expected_filename'] \
                if downloaded else None
            filename_thumbnail = data['filename_thumbnail'] \
                if downloaded else None
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                meta['channel'] = playlist.bot.get_channel(
                    data['meta']['channel']['id'])

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(
                    data['meta']['author']['id'])

            entry = cls(playlist, url, title, duration, start_seconds,
                        expected_filename, filename_thumbnail, **meta)
            entry.filename = filename

            return entry
        except Exception as error:
            LOG.error('Could not load %s', cls.__name__, exc_info=error)

    # noinspection PyTypeChecker
    async def _download(self):
        if self._is_downloading:
            return

        self._is_downloading = True
        try:
            # Ensure the folder that we're going to move into exists.
            if not os.path.exists(self.download_folder):
                os.makedirs(self.download_folder)

            if self.expected_filename:
                # self.expected_filename:
                # audio_cache\youtube-9R8aSKwTEMg-NOMA_-_Brain_Power.m4a
                extractor = os.path.basename(self.expected_filename).split('-')[0]
                # if os.name == 'nt':
                #     extractor = (self.expected_filename.split('-')[0]).rsplit('\\', 1)[-1]
                # else:
                #     extractor = (self.expected_filename.split('-')[0]).rsplit('/', 1)[-1]

                # the generic extractor requires special handling
                if extractor == 'generic':
                    LOG.debug('Handling generic')
                    # remove thumbnail images from list
                    img_pattern = re.compile(
                        r'(\.(jpg|jpeg|png|gif|bmp))$', flags=re.IGNORECASE)
                    flistdir = [f.rsplit('-', 1)[0] for f in
                                os.listdir(self.download_folder)
                                if not img_pattern.search(f)]
                    expected_fname_noex = os.path.basename(
                        self.expected_filename).rsplit('.', 1)

                    if expected_fname_noex in flistdir:
                        try:
                            rsize = int(await get_header(
                                self.playlist.bot.aiosession,
                                self.url, 'CONTENT-LENGTH'))
                        except:
                            rsize = 0

                        lfile = os.path.join(
                            self.download_folder,
                            os.listdir(self.download_folder)[
                                flistdir.index(expected_fname_noex)]
                        )

                        LOG.debug('Resolved %s to %s', self.expected_filename, lfile)
                        lsize = os.path.getsize(lfile)
                        LOG.debug('Remote size: %s Local size: %s', rsize, lsize)

                        if lsize != rsize:
                            await self._really_download(hash=True)
                        else:
                            LOG.debug('[Download] Cached: %s', self.url)
                            self.filename = lfile

                    else:
                        LOG.debug('File not found in cache (%s)', expected_fname_noex)
                        await self._really_download(hash=True)

                else:
                    img_pattern = re.compile(
                        r'(\.(jpg|jpeg|png|gif|bmp))$', flags=re.IGNORECASE)
                    ldir = [f for f in os.listdir(
                        self.download_folder) if not img_pattern.search(f)]
                    flistdir = [f.rsplit('.', 1)[0] for f in ldir]
                    expected_fname_base = os.path.basename(self.expected_filename)
                    expected_fname_noex = expected_fname_base.rsplit('.', 1)[0]

                    # idk wtf this is but its probably legacy code
                    # or i have youtube to blame for changing shit again
                    if expected_fname_base in ldir:
                        self.filename = os.path.join(
                            self.download_folder, expected_fname_base)
                        img_pattern = re.compile(
                                r'({}\.(jpg|jpeg|png|gif|bmp))$'.format(
                                    expected_fname_noex), flags=re.IGNORECASE)
                        self.filename_thumbnail = next(
                                os.path.join(self.download_folder, f)
                                for f in os.listdir(self.download_folder) if img_pattern.search(f))
                        LOG.info('Download cached: %s', self.url)

                    elif expected_fname_noex in flistdir:
                        LOG.info(
                            'Download cached (different extension): %s', self.url)
                        self.filename = os.path.join(
                            self.download_folder,
                            ldir[flistdir.index(expected_fname_noex)])
                        LOG.debug('Expected %s, got %s',
                                  self.expected_filename.rsplit('.', 1)[-1],
                                  self.filename.rsplit('.', 1)[-1]
                                 )
                    else:
                        await self._really_download()
            else:
                # For cases where Config - SaveVideos = no and the bot resumes after a restart.
                LOG.debug('Config - SaveVideos = no: Downloading the song again!')
                await self._really_download()

            # Trigger ready callbacks.
            self._for_each_future(lambda future: future.set_result(self))

        except Exception as error:
            traceback.print_exc()
            self._for_each_future(lambda future: future.set_exception(error))

        finally:
            self._is_downloading = False

    # noinspection PyShadowingBuiltins
    async def _really_download(self, *, hash=False):
        LOG.info("Download started: '%s' \[%s\]" % (self.title, self.url))

        try:
            result = await self.playlist.downloader.extract_info(
                self.playlist.loop, self.url, download=True)
        except Exception as error:
            raise ExtractionError(error)

        LOG.info("Download complete: '%s' \[%s\]" % (self.title, self.url))

        if result is None:
            LOG.critical('YTDL has failed, everyone panic')
            raise ExtractionError('ytdl broke and hell if I know why')
            # What the fuck do I do now?

        self.filename = unhashed_fname = \
            self.playlist.downloader.ytdl.prepare_filename(result)

        # Search for file name with an image suffix
        img_pattern = re.compile(
            r'({}\.(jpg|jpeg|png|gif|bmp))$'.format(
                os.path.basename(self.filename).rsplit('.', 1)[0]), flags=re.IGNORECASE)
        self.filename_thumbnail = next(
            os.path.join(self.download_folder, f)
            for f in os.listdir(self.download_folder) if img_pattern.search(f))
        if hash:
            # insert the 8 last characters of the file hash to the file name to
            # ensure uniqueness
            self.filename = md5sum(unhashed_fname, 8).join(
                '-.').join(unhashed_fname.rsplit('.', 1))

            if os.path.isfile(self.filename):
                # Oh bother it was actually there.
                os.unlink(unhashed_fname)
            else:
                # Move the temporary file to it's final location.
                os.rename(unhashed_fname, self.filename)

    def set_start(self, sec):
        if sec > self.duration or sec < 0:
            return False

        self.start_seconds = sec
        return True


class StreamPlaylistEntry(BasePlaylistEntry):
    """ TODO """

    def __init__(self, playlist, url, title, *, destination=None, **meta):
        super().__init__()

        self.playlist = playlist
        self.url = url
        self.title = title
        self.destination = destination
        self.duration = 0
        self.meta = meta

        if self.destination:
            self.filename = self.destination

    def __json__(self):
        return self._enclose_json({
            'version': 1,
            'url': self.url,
            'filename': self.filename,
            'title': self.title,
            'destination': self.destination,
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for name, obj in self.meta.items() if obj
            }
        })

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            destination = data['destination']
            filename = data['filename']
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                channel = playlist.bot.get_channel(
                    data['meta']['channel']['id'])
                meta['channel'] = channel or data['meta']['channel']['name']

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(
                    data['meta']['author']['id'])

            entry = cls(playlist, url, title, destination=destination, **meta)
            if not destination and filename:
                entry.filename = destination

            return entry
        except Exception as error:
            LOG.error('Could not load %s', cls.__name__, exc_info=error)

    # noinspection PyMethodOverriding
    async def _download(self, *, fallback=False):
        self._is_downloading = True

        url = self.destination if fallback else self.url

        try:
            result = await self.playlist.downloader.extract_info(
                self.playlist.loop, url, download=False)
        except Exception as error:
            if not fallback and self.destination:
                return await self._download(fallback=True)

            raise ExtractionError(error)
        else:
            self.filename = result['url']
            # I might need some sort of events or hooks or shit
            # for when ffmpeg inevitebly fucks up and i have to restart
            # although maybe that should be at a slightly lower level
        finally:
            self._is_downloading = False
