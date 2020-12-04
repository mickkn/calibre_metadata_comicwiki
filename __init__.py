#!/usr/bin/env python
from __future__ import (unicode_literals, division, absolute_import, print_function)

__license__ = 'GPL v3'
__copyright__ = '2020, Mick Kirkegaard (mickkn@gmail.com)'
__docformat__ = 'restructuredtext el'

import socket
import time
import datetime
import json
try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue
from six import text_type as unicode
from html5_parser import parse
from lxml.html import fromstring, tostring
from threading import Thread
from calibre.ebooks.metadata.sources.base import Source
from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars

# This is a metadata plugin for Calibre. It has been made and tested on Calibre 5.4.2
# Most of the stuff is taken from the Goodreads plugin and Biblionet plugin.
# I've just gathered everything in one __init__.py file.

class ComicWiki(Source):
    name = 'ComicWiki'
    description = ('Downloads Metadata and Covers from ComicWiki.dk based on Title')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Mick Kirkegaard'
    version = (0, 0, 1)
    minimum_calibre_version = (5, 0, 1)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['identifier:comicwiki','identifier:isbn', 'title', 'authors', 'series', 'tags', 'comments', 'publisher', 'pubdate'])

    supports_gzip_transfer_encoding = True

    ID_NAME = 'comicwiki'
    BASE_URL = 'https://www.google.com/search?q=site:comicwiki.dk '

    def get_book_url(self, identifiers):
        comicwiki_id = identifiers.get(self.ID_NAME, None)
        if comicwiki_id:
            return ('ComicWiki', comicwiki_id, self.url)

    def identify(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        '''
        Redefined identity() function
        '''
        # Create matches list
        google_matches = []
        matches = []

        # Initialize browser object
        br = self.browser
        
        # Get matches for title + author
        if title and authors:
            log.info("    Found authors and title: %s - %s" % (authors, title))
            google_matches.append('%s%s+%s' % (ComicWiki.BASE_URL, authors[0].replace(" ", "+"), title.replace(" ", "+").replace("-","+")))
            google_raw = br.open_novisit(google_matches[0], timeout=30).read().strip()
            google_root = parse(google_raw)
            google_nodes = google_root.xpath('(//div[@class="g"])//a/@href')
            for url in google_nodes[:4]:
                matches.append(url)
        # Get matches for only title (google kinda like to find match for only authors)
        if title:
            log.info("    Only found title: %s" % title)
            google_matches.append('%s%s' % (ComicWiki.BASE_URL, title.replace(" ", "+").replace("-","+")))
            google_raw = br.open_novisit(google_matches[0], timeout=30).read().strip()
            google_root = parse(google_raw)
            google_nodes = google_root.xpath('(//div[@class="g"])//a/@href')
            for url in google_nodes[:4]:
                matches.append(url)
        # Return if no Title
        if abort.is_set():
            return

        # Report the matches
        log.info("    Matches are: ", matches)

        # Setup worker thread
        workers = [Worker(url, result_queue, br, log, i, self) for i, url in enumerate(matches)]

        # Start working
        for w in workers:
            w.start()
            # Delay a little for every worker
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None

    def get_cached_cover_url(self, identifiers):
        '''
        Redefined get_cached_cover_url() function
        Just fetch cached the cover url based on isbn, we don't
        use a ComicWiki id in this plugin yet.
        '''
        isbn = identifiers.get('isbn', None)
        url = self.cached_identifier_to_cover_url(isbn)
        return url

    def download_cover(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        '''
        Redefined get_cached_cover_url() function
        Stolen from Goodreads.
        '''
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                          identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        br = self.browser
        log('    Downloading cover from:', cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)

def parse_comments(root):
    '''
    Function for parsing comments and clean them up a little
    Re-written script from the Goodreads script
    '''
    # Look for description
    description_node = root.xpath('(//div[@class="product-page-block"]//p)[1]')
    if description_node:
        desc = description_node[0] if len(description_node) == 1 else description_node[1]
        less_link = desc.xpath('a[@class="actionLinkLite"]')
        if less_link is not None and len(less_link):
            desc.remove(less_link[0])
        comments = tostring(desc, method='html', encoding=unicode).strip()
        while comments.find('  ') >= 0:
            comments = comments.replace('  ', ' ')
        if "Fil størrelse:" in comments:
            comments = comments.replace(comments.split(".")[-1], "</p>")
        comments = sanitize_comments_html(comments)
        return comments

class Worker(Thread):  # Get details
    '''
    Get book details from ComicWiki book page in a separate thread
    '''
    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.title = None
        self.isbn = None
        self.daemon = True
        self.url = url
        self.result_queue = result_queue
        self.log = log
        self.language = None
        self.timeout = timeout
        self.relevance = relevance
        self.plugin = plugin
        self.browser = browser.clone_browser()
        self.cover_url = None
        self.authors = []
        self.comments = None
        self.pubdate = None
        self.series = None
        self.series_index = None

        # Mapping language to something calibre understand.
        lm = {
            'eng': ('English', 'Engelsk'),
            'dan': ('Danish', 'Dansk'),
        }
        self.lang_map = {}
        for code, names in lm.items():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        self.log.info("    Worker.run: self: ", self)
        try:
            self.get_details()
        except:
            self.log.exception('get_details() failed for url: %r' % self.url)

    def get_details(self):
        '''
        The get_details() function for stripping the website for all information
        '''
        self.log.info("    Worker.get_details:")
        self.log.info("        self:     ", self)
        self.log.info("        self.url: ", self.url)

        # Parse the html code from the website
        try:
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        # Do some error handling if it fails to read data
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and e.getcode() == 404:
                self.log.error('URL malformed: %r' % self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Bookmeta for ComicWiki timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r' % self.url
                self.log.exception(msg)
            return

        # Do some error handling if the html code returned 404
        if "<title>404 - " == raw:
            self.log.error('URL malformed: %r' % self.url)
            return

        # Clean the html data a little
        try:
            root = parse(raw)
        except:
            self.log.error("Error cleaning HTML")
            return

        # Get the title of the book
        try:
            title_node = root.xpath('//h1[@class="firstHeading"]')
            self.title = title_node[0].text.strip()
            #print(self.title)
        except:
            self.log.exception('Error parsing title for url: %r' % self.url)

        # Get the author of the book
        try:
            author_node = root.xpath('//td[contains(text(),"Forfatter:")]/following-sibling::td/a')
            designer_node = root.xpath('//td[contains(text(),"Tegner:")]/following-sibling::td/a')
            #print(author_node[0].text.strip())
            if author_node[0].text is not None:
                if author_node[0].text.strip() not in self.authors:
                    self.authors.append(author_node[0].text.strip())
            if designer_node[0].text.strip() not in self.authors:
                self.authors.append(designer_node[0].text.strip())
            #print(type(self.authors))
            #print(self.authors)
        except:
            self.log.exception('Error parsing authors for url: %r' % self.url)
            self.authors = None

        # Get the ISBN number from the site
        try:
            isbn_node = root.xpath('//a[@class="internal mw-magiclink-isbn"]')
            self.isbn = isbn_node[0].text.strip().replace(" ", "").replace("ISBN", "").replace("-", "")
        except:
            self.log.exception('Error parsing isbn for url: %r' % self.url)
            self.isbn = None

        # Get the comments/blurb for the book
        try:
            comment_node = root.xpath('//div[@class="notice metadata"]/following-sibling::p | \
                                       //span[@id="Forlagets_resumé"]/parent::h2/following-sibling::p | \
                                       //span[@id="Bibliotekernes_resumé"]/parent::h2/following-sibling::p')
            #comment_node = root.xpath('//span[@id="Forlagets_resumé"]/parent::h2/following-sibling::p')
            for nodes in comment_node:
                if not self.comments:
                    self.comments = nodes.text
                elif nodes.text is not None:
                    self.comments = self.comments + " " + nodes.text
        except:
            self.log.exception('Error parsing comments for url: %r' % self.url)
            self.comments = None

        # Parse the cover url for downloading the cover.
        try:
            image_sub_url = root.xpath('//div[@class="aib-image"]//img/@src')
            self.cover_url = "https://comicwiki.dk" + image_sub_url[0]
            self.log.info('    Parsed URL for cover: %r' % self.cover_url)
            self.plugin.cache_identifier_to_cover_url(self.isbn, self.cover_url)
        except:
            self.log.exception('Error parsing cover for url: %r' % self.url)
            self.has_cover = bool(self.cover_url)

        # Get the publisher name
        try:
            publisher_nodes = root.xpath('//table[contains(@id,"udgivelser")]//li//a')
            self.publisher = publisher_nodes[0].text.strip()
        except:
            self.log.exception('Error parsing publisher for url: %r' % self.url)

        # Set series
        try:
            series_node = root.xpath('//div[@class="NavHead"]/a')
            series_index_node =root.xpath('//span[@class="nr"]')
            self.series = series_node[0].text
            self.series_index = series_index_node[0].text
            #print("Series: %s %s" % (self.series, self.series_index))
        except:
            self.log.exception('Error parsing series data for url: %r' % self.url)

        # Get the publisher date
        try:
            releases = []
            years = []
            #pubdate_node = root.xpath('(//dl[@class="product-info-list"]//dd)[2]') # Format dd-mm-yyyy
            year_nodes = root.xpath('//table[contains(@id,"udgivelser")]//li') # Format dd-mm-yyyy
            #release_years = year_nodes[0].text.strip()
            for item in year_nodes:
                if item.text is not None:
                    years = item.text.strip().replace(":","").replace(",", "").split(" ")
                for year in years:
                    releases.append(int(year))
            year_str = str(min(releases))
            date_str = f"01-01-{year_str}"
            #print(date_str)
            format_str = '%d-%m-%Y' # The format
            self.pubdate = datetime.datetime.strptime(date_str, format_str)
        except:
            self.log.exception('Error parsing published date for url: %r' % self.url)

        # Setup the metadata
        meta_data = Metadata(self.title, self.authors)
        meta_data.set_identifier('comicwiki', self.url)
        meta_data.set_identifier('isbn', self.isbn)

        # Set ISBN
        if self.isbn:
            try:
                meta_data.isbn = self.isbn
            except:
                self.log.exception('Error loading ISBN')
        # Set relevance
        if self.relevance:
            try:
                meta_data.source_relevance = self.relevance
            except:
                self.log.exception('Error loading relevance')
        # Set cover url
        if self.cover_url:
            try:
                meta_data.cover_url = self.cover_url
            except:
                self.log.exception('Error loading cover_url')
        # Set publisher
        if self.publisher:
            try:
                meta_data.publisher = self.publisher
            except:
                self.log.exception('Error loading publisher')
        # Set comments/blurb
        if self.comments:
            try:
                meta_data.comments = self.comments
            except:
                self.log.exception("Error loading comments")
        # Set series
        if self.series:
            try:
                meta_data.series = self.series
                meta_data.series_index = self.series_index
            except:
                self.log.exception("Error loading series and/or series index")
        # Set tags
        meta_data.tags = ('Comic', 'Graphic Novel')        
        # Set publisher data
        if self.pubdate:
            try:
                meta_data.pubdate = self.pubdate
            except:
                self.log.exception('Error loading pubdate')

        # Put meta data
        self.plugin.clean_downloaded_metadata(meta_data)
        self.result_queue.put(meta_data)

if __name__ == '__main__':  # tests
    # To run these test use:
    # calibre-customize -b . ; calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin, title_test, authors_test)

    tests = [

            (  # A comic
                {
                'identifiers': {'title': 'Den forkælede prinsesse', 'authors': 'Thom Roep & Piet Wijn'}, 
                'title': 'Den forkælede prinsesse', 
                'authors': ['Thom Roep', 'Piet Wijn']
                },[
                    title_test('Den forkælede prinsesse', exact=True),
                    authors_test(['Thom Roep', 'Piet Wijn'])]
            ),

            (  # A book with a title and author
                {
                'identifiers': {'title': 'Klo', 'authors': 'Régis Loisel'}, 
                'title': 'Klo', 
                'authors': ['Régis Loisel']
                },[
                    title_test('Klo', exact=True),
                    authors_test(['Régis Loisel'])]
            )

            ]

    test_identify_plugin(ComicWiki.name, tests)
