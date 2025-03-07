# -*- coding: utf-8 -*-
"""Custom subliminal addic7ed.com subtitle provider module."""

import hashlib
import logging
import re

from babelfish import Language

from guessit import guessit

from requests import Session

from subliminal.cache import SHOW_EXPIRATION_TIME, region
from subliminal.exceptions import ConfigurationError, DownloadLimitExceeded
from subliminal.matches import guess_matches
from subliminal.providers import ParserBeautifulSoup, Provider
from subliminal.subtitle import Subtitle, fix_line_ending
from subliminal.utils import sanitize
from subliminal.video import Episode

logger = logging.getLogger(__name__)

# language_converters.register('addic7ed = subliminal.converters.addic7ed:Addic7edConverter')

# Series cell matching regex
show_cells_re = re.compile(b'<td class="vr">.*?</td>', re.DOTALL)

#: Series header parsing regex
series_year_re = re.compile(r'^(?P<series>[ \w\'.:(),*&!?-]+?)(?: \((?P<year>\d{4})\))?$')


class Addic7edSubtitle(Subtitle):
    """Addic7ed Subtitle."""

    provider_name = 'addic7ed'

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title, year, version,
                 download_link):
        super(Addic7edSubtitle, self).__init__(language, hearing_impaired=hearing_impaired, page_link=page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.year = year
        self.version = version
        self.download_link = download_link

    @property
    def id(self):
        """Get id."""
        return self.download_link

    @property
    def info(self):
        """Get info."""
        return '{series}{yopen}{year}{yclose} s{season:02d}e{episode:02d}{topen}{title}{tclose}{version}'.format(
            series=self.series, season=self.season, episode=self.episode, title=self.title, year=self.year or '',
            version=self.version, yopen=' (' if self.year else '', yclose=')' if self.year else '',
            topen=' - ' if self.title else '', tclose=' - ' if self.version else ''
        )

    def get_matches(self, video):
        """Get matches."""
        # series name
        matches = guess_matches(video, {
            'title': self.series,
            'season': self.season,
            'episode': self.episode,
            'episode_title': self.title,
            'year': self.year,
            'release_group': self.version,
        })

        # resolution
        if video.resolution and self.version and video.resolution in self.version.lower():
            matches.add('resolution')
        # other properties
        if self.version:
            matches |= guess_matches(video, guessit(self.version, {'type': 'episode'}), partial=True)

        return matches


class Addic7edProvider(Provider):
    """Addic7ed Provider."""

    languages = {Language('por', 'BR')} | {Language(l) for l in [
        'ara', 'aze', 'ben', 'bos', 'bul', 'cat', 'ces', 'dan', 'deu', 'ell', 'eng', 'eus', 'fas', 'fin', 'fra', 'glg',
        'heb', 'hrv', 'hun', 'hye', 'ind', 'ita', 'jpn', 'kor', 'mkd', 'msa', 'nld', 'nor', 'pol', 'por', 'ron', 'rus',
        'slk', 'slv', 'spa', 'sqi', 'srp', 'swe', 'tha', 'tur', 'ukr', 'vie', 'zho'
    ]}
    video_types = (Episode,)
    server_url = 'http://www.addic7ed.com/'
    subtitle_class = Addic7edSubtitle

    def __init__(self, username=None, password=None):
        if any((username, password)) and not all((username, password)):
            raise ConfigurationError('Username and password must be specified')

        self.username = username
        self.password = hashlib.md5(password.encode('utf-8')).hexdigest()
        self.logged_in = False
        self.cookies = {'wikisubtitlesuser': self.username, 'wikisubtitlespass': self.password}

    def initialize(self):
        """Initialize Addic7edProvider provider."""
        self.session = Session()
        self.session.headers['User-Agent'] = self.user_agent

        # login
        if self.username and self.password:
            logger.debug('Logged in')
            self.logged_in = True

    def terminate(self):
        """Terminate."""
        # logout
        logger.debug('Logged out')
        self.logged_in = False

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _get_show_ids(self):
        """Get the ``dict`` of show ids per series by querying the `shows.php` page.

        :return: show id per series, lower case and without quotes.
        :rtype: dict
        """
        # get the show page
        logger.info('Getting show ids')
        r = self.session.get(self.server_url + 'shows.php', timeout=20, cookies=self.cookies)
        r.raise_for_status()

        # LXML parser seems to fail when parsing Addic7ed.com HTML markup.
        # Last known version to work properly is 3.6.4 (next version, 3.7.0, fails)
        # Assuming the site's markup is bad, and stripping it down to only contain what's needed.
        show_cells = re.findall(show_cells_re, r.content)
        if show_cells:
            soup = ParserBeautifulSoup(b''.join(show_cells), ['lxml', 'html.parser'])
        else:
            # If RegEx fails, fall back to original r.content and use 'html.parser'
            soup = ParserBeautifulSoup(r.content, ['html.parser'])

        # populate the show ids
        show_ids = {}
        for show in soup.select('td.vr > h3 > a[href^="/show/"]'):
            show_ids[sanitize(show.text)] = int(show['href'][6:])
        logger.debug('Found %d show ids', len(show_ids))

        return show_ids

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _search_show_id(self, series, year=None):
        """Search the show id from the `series` and `year`.

        :param str series: series of the episode.
        :param year: year of the series, if any.
        :type year: int
        :return: the show id, if found.
        :rtype: int
        """
        # addic7ed doesn't support search with quotes
        series = series.replace("'", ' ')

        # build the params
        series_year = '%s %d' % (series, year) if year is not None else series
        params = {'search': series_year, 'Submit': 'Search'}

        r = self.session.get('http://www.addic7ed.com/srch.php', params=params, timeout=30, cookies=self.cookies)

        # make the search
        logger.info('Searching show ids with %r', params)
        r.raise_for_status()
        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])

        # get the suggestion
        suggestion = soup.select('span.titulo > a[href^="/show/"]')
        if not suggestion:
            logger.warning('Show id not found: no suggestion')
            return None
        if not sanitize(suggestion[0].i.text.replace("'", ' ')) == sanitize(series_year):
            logger.warning('Show id not found: suggestion does not match')
            return None
        show_id = int(suggestion[0]['href'][6:])
        logger.debug('Found show id %d', show_id)

        return show_id

    def get_show_id(self, series, year=None, country_code=None):
        """Get the best matching show id for `series`, `year` and `country_code`.

        First search in the result of :meth:`_get_show_ids` and fallback on a search with :meth:`_search_show_id`.
        :param str series: series of the episode.
        :param year: year of the series, if any.
        :type year: int
        :param country_code: country code of the series, if any.
        :type country_code: str
        :return: the show id, if found.
        :rtype: int
        """
        series_sanitized = sanitize(series).lower()
        show_ids = self._get_show_ids()
        show_id = None

        # attempt with country
        if country_code:
            logger.debug('Getting show id with country')
            show_id = show_ids.get('%s %s' % (series_sanitized, country_code.lower()))

        # attempt with year
        if not show_id and year:
            logger.debug('Getting show id with year')
            show_id = show_ids.get('%s %d' % (series_sanitized, year))

        # attempt clean
        if not show_id:
            logger.debug('Getting show id')
            show_id = show_ids.get(series_sanitized)

        # search as last resort
        if not show_id:
            logger.info('Series %s not found in show ids', series)
            show_id = self._search_show_id(series)

        return show_id

    def query(self, show_id, series, season, year=None, country=None):
        """Query provider to get all subitles for a specific show + season."""
        # get the page of the season of the show
        logger.info('Getting the page of show id %d, season %d', show_id, season)
        r = self.session.get(self.server_url + 'show/%d' % show_id, params={'season': season}, timeout=60, cookies=self.cookies)
        r.raise_for_status()

        if not r.content:
            # Provider returns a status of 304 Not Modified with an empty content
            # raise_for_status won't raise exception for that status code
            logger.debug('No data returned from provider')
            return []

        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])

        # loop over subtitle rows
        match = series_year_re.match(soup.select('#header font')[0].text.strip()[:-10])
        series = match.group('series')
        year = int(match.group('year')) if match.group('year') else None
        subtitles = []
        for row in soup.select('tr.epeven'):
            cells = row('td')

            # ignore incomplete subtitles
            status = cells[5].text
            if status != 'Completed':
                logger.debug('Ignoring subtitle with status %s', status)
                continue

            # read the item
            language = Language.fromaddic7ed(cells[3].text)
            hearing_impaired = bool(cells[6].text)
            page_link = self.server_url + cells[2].a['href'][1:]
            season = int(cells[0].text)
            episode = int(cells[1].text)
            title = cells[2].text
            version = cells[4].text
            download_link = cells[9].a['href'][1:]

            subtitle = self.subtitle_class(language, hearing_impaired, page_link, series, season, episode, title, year,
                                           version, download_link)
            logger.debug('Found subtitle %r', subtitle)
            subtitles.append(subtitle)

        return subtitles

    def list_subtitles(self, video, languages):
        """List Subitles."""
        # lookup show_id
        titles = [video.series] + video.alternative_series
        show_id = None
        for title in titles:
            show_id = self.get_show_id(title, video.year)
            if show_id is not None:
                break

        # query for subtitles with the show_id
        if show_id is not None:
            subtitles = [s for s in self.query(show_id, title, video.season, video.year)
                         if s.language in languages and s.episode == video.episode]
            if subtitles:
                return subtitles
        else:
            logger.info('No show id found for %r (%r)', video.series, {'year': video.year})

        return []

    def download_subtitle(self, subtitle):
        """Download subtitles."""
        # download the subtitle
        logger.info('Downloading subtitle %r', subtitle)
        r = self.session.get(self.server_url + subtitle.download_link, headers={'Referer': subtitle.page_link},
                             timeout=20)
        r.raise_for_status()

        if not r.content:
            # Provider returns a status of 304 Not Modified with an empty content
            # raise_for_status won't raise exception for that status code
            logger.debug('Unable to download subtitle. No data returned from provider')
            return

        # detect download limit exceeded
        if r.headers['Content-Type'] == 'text/html':
            raise DownloadLimitExceeded

        subtitle.content = fix_line_ending(r.content)
