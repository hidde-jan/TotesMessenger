import logging
import praw
import os
import re
import sqlite3
import sys
import time
import traceback

# Requests' exceptions live in .exceptions and are called errors.
from requests.exceptions import ConnectionError, HTTPError
# Praw's exceptions live in .errors and are called exceptions.
from praw.errors import APIException, ClientException, RateLimitExceeded

from urllib.parse import urlparse

from settings import IGNORED_BOTH, IGNORED_LINKS, IGNORED_SOURCES, IGNORED_USERS

TEST = True
DEBUG = True

USER_AGENT = 'TotesMessenger v0.x by /u/justcool393'
DOMAIN = 'api.reddit.com'

ARCHIVE_TIME = 6 * 30 * 24 * 60 * 60  # currently 6 months (in seconds)

loglevel = logging.DEBUG if DEBUG else logging.INFO

logging.basicConfig(level=loglevel,
                    format='[%(asctime)s] [%(levelname)s] %(message)s')

log = logging.getLogger('totes')
logging.getLogger('requests').setLevel(loglevel)

db = sqlite3.connect('totes.sqlite3')
cur = db.cursor()

r = praw.Reddit(USER_AGENT, domain=DOMAIN)

PATH_REGEX = re.compile(r'^/r/([^/]+)/comments/([a-z0-9]{6,8})(/[^/]+/([a-z0-9]{6,8}))?')

def log_error(e):
    log.error("Unexpected {}:\n{}".format(e.__class__.__name__,
                                          traceback.format_exc()))

def np(url):
    url = urlparse(url)
    return "//np.reddit.com{}".format(url.path)


class RecoverableException(Exception):
    pass


class SubmissionNotFound(RecoverableException):
    def __init__(self, id):
        self.id = id

    def __str__(self):
        return "Could not find submission {}".format(id)


class NotAComment(RecoverableException):
    pass


RECOVERABLE_EXC= (RecoverableException,
                  ConnectionError,
                  HTTPError,
                  APIException,
                  ClientException,
                  RateLimitExceeded)


class Source:
    """
    Comment or thread that has been linked to from somewhere else on reddit.
    """
    def __init__(self, url):
        self.path = urlparse(url.lower()).path
        self.id, self.subreddit = self._parse_path()

        self._submission = None
        self.author = None
        self.title = None
        self.reply = None
        self.skip = False
        self.is_new = True

    def __eq__(self, other):
        if isinstance(other, Source):
            return self.id == other.id

        return False

    def __hash__(self):
        return hash(self.id)

    @property
    def submission(self):
        if self._submission:
            return self._submission

        self._submission = r.get_info(thing_id=self.id)

        if not self._submission:
            raise SubmissionNotFound(self.id)

        return self._submission



    @property
    def is_comment(self):
        return self.id.startswith('t1')

    @property
    def is_post(self):
        return self.id.startswith('t3')

    def check_skip(self):
        if self.skip:
            return True

        cur.execute(
            "SELECT * FROM users WHERE name = ? AND skip_source = ? LIMIT 1",
            (self.author, True))

        if cur.fetchone():
            self.skip = True
            return True

        cur.execute(
            "SELECT * FROM subreddits WHERE name = ? AND skip_source = ? LIMIT 1",
            (self.subreddit, True))

        if cur.fetchone():
            self.skip = True
            return True

        return False

    def save(self):
        cur.execute("""
        REPLACE INTO sources (id, reply, subreddit, author, title, skip)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (self.id, self.reply, self.subreddit, self.author, self.title, self.skip))

        # Maybe commit?
        db.commit()

    def load(self):
        """
        Populate attributes from database and fetch corresponding submission.
        """
        cur.execute("""
        SELECT id, reply, subreddit, author, title, skip FROM sources
        WHERE id=? LIMIT 1
        """, (self.id,))

        source = cur.fetchone()

        if source:
            self.id, self.reply, self.subreddit, self.author, self.title, self.skip = source
            self.is_new = False
            return  # Return early cuz we don't need to perform an api call.

        if not self.author and self.submission.author:
            self.author = self.submission.author.name.lower()
        else:
            self.author = '[deleted]'

        if hasattr(self.submission, 'title'):
            self.title = self.submission.title
        else:
            self.title = '[comment]'

    def _parse_path(self):
        # Comments have path /r/sub/comments/xxx/title/xxx/
        # Posts have path /r/sub/comments/xxx/title/
        match = PATH_REGEX.match(self.path)

        if match:
            subreddit, post, _, comment = match.groups()
        else:
            raise NotAComment("The source {} is not a comment or post.".format(self.path))

        if comment:
            id = "t1_{}".format(comment)
        else:
            id = "t3_{}".format(post)

        return (id, subreddit)


class Link:
    def __init__(self, submission, source):
        self.submission = submission
        self.id = submission.name
        self.subreddit = submission.subreddit.display_name.lower()

        if submission.author:
            self.author = submission.author.name.lower()
        else:
            self.author = '[deleted]'

        self.title = submission.title
        self.permalink = submission.permalink
        self.source = source
        self.skip = False
        self.is_new = True

    def check_skip(self):
        if self.skip:
            return True

        cur.execute(
            "SELECT * FROM users WHERE name = ? AND skip_link = ? LIMIT 1",
            (self.author, True))

        if cur.fetchone():
            self.skip = True
            return True

        cur.execute(
            "SELECT * FROM subreddits WHERE name = ? AND skip_link = ? LIMIT 1",
            (self.subreddit, True))

        if cur.fetchone():
            self.skip = True
            return True

        return False

    def save(self):
        cur.execute("""
        REPLACE INTO links (id, source, permalink, subreddit, skip, author, title)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (self.id, self.source, self.permalink, self.subreddit, self.skip, self.author, self.title))

        # Maybe commit less often?
        db.commit()


    def load(self):
        """
        Populate attributes from database and fetch corresponding submission.
        """
        cur.execute("""
        SELECT id, source, permalink, subreddit, skip, author, title FROM links
        WHERE id=? LIMIT 1
        """, (self.id,))

        link = cur.fetchone()

        if link:
           self.id, self.source, self.permalink, self.subreddit, self.skip, self.author, self.title = link
           self.is_new = False

class Notification:
    def __init__(self, source):
        self.source = source
        self.id = source.id
        self.reply = source.reply
        self.links = []

    def should_notify(self):
        query = cur.execute("""
        SELECT subreddit, title, permalink FROM links
        WHERE source=? AND skip=?
        ORDER BY subreddit ASC, title ASC
        """, (self.id, False))

        for row in query:
            self.links.append(row)

        return any(self.links)

    def post_reply(self):
        # Render body.
        body = self._render_comment()

        if TEST:
            log.debug("""
========== COMMENT ============
Source: {}
{}
========== /COMMENT ===========
""".format(self.source.path, body))
            return True

        if self.reply:
            reply = r.get_info(thing_id=self.reply)
            reply.edit(body)
            return True

        if self.source.is_comment:
            reply = self.source.submission.reply(body)
            self.reply = reply.name

        elif self.source.is_post:
            reply = self.source.submission.add_comment(body)
            self.reply = reply.name

        self.source.reply = self.reply
        self.source.save()

        return True

    def _render_comment(self):
        parts = []
        parts.append("This thread has been linked to from another place on reddit.")

        for subreddit, title, permalink in self.links:
            parts.append("- [/r/{}] [{}]({})".format(subreddit, title, np(permalink)))

        parts.append("""
[](#footer)*^(If you follow any of the above links, respect the rules of reddit and don't vote.)
            ^\([Info](/r/TotesMessenger/wiki/)
            ^/
            ^[Contact](/message/compose/?to=\/r\/TotesMessenger))* [](#bot)
        """)

        return "\n\n".join(parts)



class Totes:

    def __init__(self, username, password, limit=25):
        self.username = username
        self.password = password
        self.limit = limit

        self._setup = False

    def run(self):
        """
        Comment replies notifying posts and comments that they have been linked
        to from somewhere else on reddit.
        """
        if not self._setup:
            raise Exception("Totes not ready yet!!!")

        log.info("Running.")

        sources = set()

        submissions = r.get_domain_listing('reddit.com', sort='new', limit=self.limit)

        for submission in submissions:
            try:
                source = Source(submission.url)
                source.load()
            except RECOVERABLE_EXC as e:
                log_error(e)
                continue

            log.debug("Got source: {}".format(submission.url))

            source.check_skip()
            source.save()

            try:
                link = Link(submission, source.id)
                link.load()
            except RECOVERABLE_EXC as e:
                log_error(e)
                continue

            log.debug("Got link: {}".format(submission.permalink))

            link.check_skip()
            link.save()

            skip_any = source.skip or link.skip
            any_new = source.is_new or link.is_new

            log.debug("Skip any: {}".format(skip_any))
            log.debug("Any new: {}".format(any_new))

            if any_new and not skip_any:
                sources.add(source)

        for source in sources:
            notification = Notification(source)

            if notification.should_notify():
                try:
                    notification.post_reply()
                except RECOVERABLE_EXC as e:
                    log_error(e)
                    continue

        log.info("Done.")

    def setup(self):
        """
        Load settings and perform setup.
        """
        self._setup_db()
        self._login()

        self._setup = True
        log.info("Totes set up.")

    def quit(self):
        """
        Teardown.
        """
        log.info("Totes goodbye!")

    def _login(self):
        """
        Create reddit session.
        """
        r.login(self.username, self.password)
        log.info("Logged in to reddit.")

    def _setup_db(self):
        """
        Create tables.
        """

        cur.execute("""
        CREATE TABLE IF NOT EXISTS subreddits (
            name         TEXT       PRIMARY KEY,
            skip_source  BOOLEAN    DEFAULT 0,
            skip_link    BOOLEAN    DEFAULT 0,
            t            TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            name         TEXT       PRIMARY KEY,
            skip_source  BOOLEAN    DEFAULT 0,
            skip_link    BOOLEAN    DEFAULT 0,
            t            TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id         TEXT       PRIMARY KEY,
            reply      TEXT       UNIQUE,
            subreddit  TEXT,
            author     TEXT,
            title      TEXT,
            skip       BOOLEAN    DEFAULT 0,
            t          TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id         TEXT       PRIMARY KEY,
            source     TEXT,
            subreddit  TEXT,
            author     TEXT,
            title      TEXT,
            permalink  TEXT,
            skip       BOOLEAN    DEFAULT 0,
            t          TIMESTAMP  DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE INDEX IF NOT EXISTS link_sources ON links (source)
        """)

        db.commit()
        log.info("Tables ready.")

        for sub in IGNORED_SOURCES:
            cur.execute("""
            INSERT OR IGNORE INTO subreddits (name, skip_source)
            VALUES (?, ?)
            """, (sub, True))

        for sub in IGNORED_BOTH:
            cur.execute("""
            INSERT OR IGNORE INTO subreddits (name, skip_source, skip_link)
            VALUES (?, ?, ?)
            """, (sub, True, True))

        for sub in IGNORED_LINKS:
            cur.execute("""
            INSERT OR IGNORE INTO subreddits (name, skip_link)
            VALUES (?, ?)
            """, (sub, True))

        for user in IGNORED_USERS:
            cur.execute("""
            INSERT OR IGNORE INTO users (name, skip_link) VALUES (?, ?)
            """, (user, True))

        db.commit()
        log.info("Default settings setup.")


if __name__ == "__main__":
    username = os.environ.get("REDDIT_USERNAME")
    password = os.environ.get("REDDIT_PASSWORD")
    wait = int(os.environ.get("WAIT", 30))
    limit = int(os.environ.get("LIMIT", 25))

    totes = Totes(username, password, limit)
    totes.setup()

    try:
        while True:
            try:
                totes.run()
            except RECOVERABLE_EXC as e:
                log_error(e)

            time.sleep(wait)
    except KeyboardInterrupt:
        pass

    totes.quit()
    db.close()
    exit(0)

