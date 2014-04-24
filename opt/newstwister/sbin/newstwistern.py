#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys, os, time, json, argparse
import urllib, select
import oauth2 as oauth
import signal, atexit
import ctypes

from pprint import pformat

from zope.interface import Interface, implements

from twisted.internet import reactor
from twisted.internet.defer import Deferred, succeed
from twisted.internet.protocol import Protocol
from twisted.internet.ssl import ClientContextFactory
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from twisted.python.log import err

try:
    import setproctitle
except:
    if not str(sys.platform).lower().startswith('linux'):
        sys.stderr.write('either the setproctitle module has to be available, or the OS has to be Linux')
        sys.exit(1)

SAVE_URL = 'http://localhost:9200/newstwister/tweets/'

PR_SET_NAME = 15
NODE_NAME = 'newstwistern'

TODEBUG = False
DEBUGPATH = '/tmp/newstwister_node.debug'

def debug_msg(msg):
    if not TODEBUG:
        return

    try:
        fh = open(DEBUGPATH, 'a+')
        fh.write(str(msg) + '\n')
        fh.flush()
        fh.close()
    except:
        pass

def set_proc_name():
    name_set = False

    try:
        setproctitle.setproctitle(NODE_NAME)
        name_set = True
    except:
        name_set = False

    if name_set:
        return True

    try:
        libc = ctypes.cdll.LoadLibrary('libc.so.6')
        buff = ctypes.create_string_buffer(len(NODE_NAME)+1)
        buff.value = NODE_NAME
        libc.prctl(PR_SET_NAME, ctypes.byref(buff), 0, 0, 0)
    except:
        return False

    return True

class SaveSpecs():
    def __init__(self):
        self.specs = {
            'save_url': SAVE_URL
        }
    def get_specs(self):
        return self.specs

    def set_specs(self, save_url):
        self.specs = {
            'save_url': save_url
        }

    def use_specs(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('-s', '--save_url', help='url for saving the tweets')

        args = parser.parse_args()
        if args.save_url:
            self.specs['save_url'] = args.save_url

save_specs = SaveSpecs()

class Params():
    def get_post_params(self):
        global stream_filter

        post_params = {}
        post_params['stall_warnings'] = 'true'

        for one_key in stream_filter:
            try:
                post_params[one_key] = stream_filter[one_key].decode('utf-8')
            except:
                pass

        return post_params

    def get_oauth_header(self):
        global oauth_info

        oauth_consumer = oauth.Consumer(key=oauth_info['consumer_key'], secret=oauth_info['consumer_secret'])
        oauth_token = oauth.Token(key=oauth_info['access_token_key'], secret=oauth_info['access_token_secret'])

        oauth_params = {}
        oauth_params['oauth_version'] = '1.0'
        oauth_params['oauth_nonce'] = oauth.generate_nonce()
        oauth_params['oauth_timestamp'] = int(time.time())

        req_url = 'https://stream.twitter.com/1.1/statuses/filter.json' + '?' + urllib.urlencode(self.get_post_params())

        req = oauth.Request(method='POST', parameters=oauth_params, url=req_url)
        req.sign_request(oauth.SignatureMethod_HMAC_SHA1(), oauth_consumer, oauth_token)
        return req.to_header()['Authorization'].encode('utf-8')

    def get_headers(self):
        conn_headers = {}
        conn_headers['Host'] = ['stream.twitter.com']
        conn_headers['Authorization'] = [self.get_oauth_header()]
        conn_headers['User-Agent'] = ['Newstwister']
        conn_headers['Content-Type'] = ['application/x-www-form-urlencoded']

        return conn_headers

class StringProducer(object):
    implements(IBodyProducer)
 
    def __init__(self, body):
        self.body = body
        self.length = len(body)
 
    def startProducing(self, consumer):
        consumer.write(self.body)
        return succeed(None)
 
    def pauseProducing(self):
        pass
 
    def stopProducing(self):
        pass

# Part putting tweets into ES

class ElsClientContextFactory(ClientContextFactory):
    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)

class ElsResponser(Protocol):
    def __init__(self, finished):
        self.finished = finished
        self.buffer = ''
        self.count = 0

    def dataReceived(self, data):
        debug_msg('got next data from ES: ' + str(data))
        pass

    def connectionLost(self, reason):
        debug_msg('Finished receiving ES response: ' + str(reason.getErrorMessage()))
        self.finished.callback(None)

class TweetSaver(object):
    def __init__(self):
        global save_specs

        self.save_url = save_specs.get_specs()['save_url']
        if not self.save_url.endswith('/'):
            self.save_url += '/'

    def get_headers(self):
        host = self.save_url[(self.save_url.find(':')+1):]
        host = host[:host.find('/')]

        conn_headers = {}
        conn_headers['Host'] = [host]
        conn_headers['User-Agent'] = ['Newstwister']
        conn_headers['Content-Type'] = ['application/json']
        conn_headers['Accept'] = ['application/json']

        return conn_headers

    def save_tweet(self, tweet):
        tweet_id = tweet.get('id_str')
        if not tweet_id:
            return False
        save_data = {}
        save_data['request'] = None
        save_data['type'] = 'stream'
        save_data['endpoint'] = endpoint
        save_data['filter'] = stream_filter
        save_data['tweet'] = tweet

        tweet_data = json.dumps(save_data)

        contextFactory = ElsClientContextFactory()
        agent = Agent(reactor, contextFactory)

        send_url = self.save_url + str(tweet_id)

        d_es = agent.request(
            'POST',
            send_url,
            Headers(self.get_headers()),
            StringProducer(tweet_data))

        borders = ElsResponseBorders()
        d_es.addCallback(borders.cbRequest)
        d_es.addBoth(borders.cbShutdown)

        return d_es

class ElsResponseBorders():
    def cbRequest(self, response):

        debug_msg('Els response version: ' + str(response.version))
        debug_msg('Els response code: ' + str(response.code))
        debug_msg('Els response phrase: ' + str(response.phrase))
        debug_msg('Els response headers:')
        debug_msg(pformat(list(response.headers.getAllRawHeaders())))

        finished = Deferred()
        response.deliverBody(ElsResponser(finished))
        return finished

    def cbShutdown(self, ignored):
        debug_msg(str(ignored))
        debug_msg('shutting down els connection')
        pass

# Part taking tweets from Twitter

class TwtClientContextFactory(ClientContextFactory):
    def getContext(self, hostname, port):
        return ClientContextFactory.getContext(self)

class TweetProcessor(Protocol):
    def __init__(self, finished):
        self.finished = finished
        self.buffer = ''
        self.count = 0

    def _process_tweet(self, data):

        self.buffer += data
        if data.endswith('\r\n') and self.buffer.strip(): # some finished message
            message = json.loads(self.buffer)
            self.buffer = ''

            # status messages
            if message.get('limit'): # error (not a tweet), over the rate limit
                debug_msg('rate limit over, count of missed tweets: ' + str(message['limit'].get('track')))
                pass
            elif message.get('disconnect'): # error (not a tweet), got disconnected
                debug_msg('disconnected: ' + str(message['disconnect'].get('reason')))
                pass
                # should restart the read cycle!
            elif message.get('warning'): # warning (not a tweet)
                debug_msg('warning: ' + str(message['warning'].get('message')))
                pass

            # actual tweet
            else:
                # putting the tweet into elastic search
                tws = TweetSaver()
                tws.save_tweet(message)

                '''
                # outputting the tweet, development purposes only
                tid = message.get('id_str')
                user = message.get('user')
                uid = str(user['id_str']) if (user and ('id_str' in user)) else ''
                uname = str(user['name'].encode('utf-8')) if (user and ('name' in user)) else ''
                ulocation = str(user['location'].encode('utf-8')) if (user and ('location' in user)) else ''

                t_coords = str(message.get('coordinates'))
                t_geo = str(message.get('geo'))
                t_place = str(message.get('place'))

                msg_beg = 'https://twitter.com/cdeskdev/status/' + str(tid) + '\n'
                msg_geo = 'coords: ' + t_coords + ', geo: ' + t_geo + ', place: ' + t_place + '\n'
                msg_mid = '' + uid + '/' + uname + '/' + ulocation + ': '
                msg_end = message.get('text').encode('utf-8')
                print(msg_beg + msg_geo + msg_mid + msg_end)
                print(str(message.get('entities')) + '\n')
                '''

    def dataReceived(self, data):

        self._process_tweet(data)

        '''
        #development purposes only
        self.count += 1
        print('count: ' + str(self.count))
        '''

    def connectionLost(self, reason):
        debug_msg('Finished receiving Twitter stream: ' + str(reason.getErrorMessage()))
        self.finished.callback(None)

class TwtResponseBorders():
    def cbRequest(self, response):

        debug_msg('Twt response version:' + str(response.version))
        debug_msg('Twt response code:' + str(response.code))
        debug_msg('Twt response phrase:' + str(response.phrase))
        debug_msg('Twt response headers:')
        debug_msg(pformat(list(response.headers.getAllRawHeaders())))

        if '200' != str(response.code):
            close_reactor()
            return

        finished = Deferred()
        response.deliverBody(TweetProcessor(finished))
        return finished

    def cbShutdown(self, ignored):
        debug_msg(ignored)
        debug_msg('shutting twt down')
        close_reactor()

def close_reactor():
    try:
        reactor.disconnectAll()
    except:
        pass

    try:
        reactor.stop()
    except:
        pass

    cleanup()

def make_stream_connection():
    params = Params()
    post_data = urllib.urlencode(params.get_post_params())

    contextFactory = TwtClientContextFactory()
    agent = Agent(reactor, contextFactory)

    d = agent.request(
        'POST',
        'https://stream.twitter.com/1.1/statuses/filter.json',
        Headers(params.get_headers()),
        StringProducer(post_data))

    borders = TwtResponseBorders()
    d.addCallback(borders.cbRequest)
    d.addBoth(borders.cbShutdown)

    return d

# General script passage

def process_quit(signal_number, frame):
    global d

    try:
        d.cancel()
    except:
        pass

    close_reactor()
    cleanup()

def cleanup():
    debug_msg(str(os.getpid()))
    debug_msg('stopping the process')
    os._exit(0)

endpoint = {
    'endpoint_id': None
}
oauth_info = {
    'consumer_key': None,
    'consumer_secret': None,
    'access_token_key': None,
    'access_token_secret': None
}
stream_filter = {}
stream_filter_basic = [
    'follow',
    'track',
    'locations'
]
stream_filter_other = [
    'filter_level',
    'language'
]

if __name__ == '__main__':

    if not set_proc_name():
        sys.exit(1)

    save_specs.use_specs()

    signal.signal(signal.SIGINT, process_quit)
    signal.signal(signal.SIGTERM, process_quit)

    atexit.register(cleanup)

    twitter_param_list = []
    while True:
        rfds, wfds, efds = select.select([sys.stdin], [], [], 1)
        if rfds:
            twitter_param_list.append(sys.stdin.readline())
        else:
            break

    is_correct = True
    twitter_params = None

    if not twitter_param_list:
        is_correct = False

    if is_correct:
        try:
            twitter_params = json.loads('\n'.join(twitter_param_list))
            if type(twitter_params) is not dict:
                is_correct = False
            if not twitter_params:
                is_correct = False
        except:
            is_correct = False

    if is_correct:
        try:
            if not 'oauth_info' in twitter_params:
                is_correct = False
            elif type(twitter_params['oauth_info']) is not dict:
                is_correct = False

            if not 'stream_filter' in twitter_params:
                is_correct = False
            elif type(twitter_params['stream_filter']) is not dict:
                is_correct = False

            if not 'endpoint' in twitter_params:
                is_correct = False
            elif type(twitter_params['endpoint']) is not dict:
                is_correct = False
        except:
            is_correct = False

    if is_correct:
        for part in oauth_info:
            if not part in twitter_params['oauth_info']:
                is_correct = False
                break
            if not twitter_params['oauth_info'][part]:
                is_correct = False
                break
            try:
                oauth_info[part] = str(twitter_params['oauth_info'][part])
            except:
                is_correct = False
                break

    if is_correct:
        is_correct = False
        for part in stream_filter_basic:
            if part in twitter_params['stream_filter']:
                if twitter_params['stream_filter'][part]:
                    try:
                        stream_filter[part] = str(twitter_params['stream_filter'][part])
                    except:
                        is_correct = False
                        break
                    if stream_filter[part]:
                        is_correct = True

    if is_correct:
        for part in stream_filter_other:
            if part in twitter_params['stream_filter']:
                if twitter_params['stream_filter'][part]:
                    try:
                        stream_filter[part] = str(twitter_params['stream_filter'][part])
                    except:
                        is_correct = False
                        break

    if is_correct:
        for part in endpoint:
            if not part in twitter_params['endpoint']:
                is_correct = False
                break
            if not twitter_params['endpoint'][part]:
                is_correct = False
                break
            try:
                endpoint[part] = str(twitter_params['endpoint'][part])
            except:
                is_correct = False
                break

    if is_correct:
        try:
            d = make_stream_connection()
        except:
            is_correct = False

    if is_correct:
        reactor.run()

