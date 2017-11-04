from elasticsearch_dsl.connections import connections
from elasticsearch import helpers
from elasticsearch_dsl import Search
from elasticsearch.exceptions import ConnectionError, NotFoundError
from glob import glob
import gzip
import logging
import json
import argparse
from datetime import datetime
from time import sleep
import os
import itertools
import math

from models import TweetIndex, to_tweet, DatasetIndex, to_dataset, DatasetDocType, get_tweets_index_name
from utils import read_json, short_uid

log = logging.getLogger(__name__)

connections.create_connection(hosts=['elasticsearch'], timeout=90)

CONNECTION_ERROR_TRIES = 30
CONNECTION_ERROR_SLEEP = 30


def find_files(path):
    """
    Returns (.json files, .json.gz files, .txt files) found in path.
    """
    json_filepaths = glob('{}/*.json'.format(path))
    dataset_filepath = os.path.join(path, 'dataset.json')
    if dataset_filepath in json_filepaths:
        json_filepaths.remove(dataset_filepath)
    return (json_filepaths,
            glob('{}/*.json.gz'.format(path)),
            glob('{}/*.txt'.format(path)))


def count_lines(json_files, json_gz_files, txt_files):
    total_lines = 0
    for filepath in json_files:
        total_lines += sum(1 for _ in open(filepath))
    for filepath in json_gz_files:
        total_lines += sum(1 for _ in gzip.open(filepath))
    for filepath in txt_files:
        total_lines += sum(1 for _ in open(filepath))
    return total_lines


def count_files(json_files, json_gz_files, txt_files):
    return len(json_files) + len(json_gz_files) + len(txt_files)


def tweet_iter(json_files, json_gz_files, txt_files, limit=None, total_tweets=None):
    counter = 0
    for filepath in json_files:
        with open(filepath) as file:
            for line in file:
                if counter % 10000 == 0:
                    log.info('{:,} of {:,} tweets'.format(counter, limit or total_tweets or 0))
                if counter == limit:
                    break
                counter += 1
                yield json.loads(line)
    for filepath in json_gz_files:
        with gzip.open(filepath) as file:
            for line in file:
                if counter % 10000 == 0:
                    log.info('{:,} of {:,} tweets'.format(counter, limit or total_tweets or 0))
                if counter == limit:
                    break
                counter += 1
                yield json.loads(line)

                # TODO: Handle hydration


def _chunker(iterable, chunk_size=500):
    """
    Splits an iterable up into chunks.
    """
    it = iter(iterable)
    while True:
        chunk = tuple(itertools.islice(it, chunk_size))
        if not chunk:
            return
        yield chunk


def delete_tweet_index(dataset_identifier):
    tweet_index = TweetIndex(dataset_identifier)
    tweet_index.delete(ignore=404)
    log.info('Deleted tweets from {}'.format(dataset_identifier))


def create_tweet_index(dataset_id, shards, replicas):
    tweet_index = TweetIndex(dataset_id, shards=shards, replicas=replicas)
    tweet_index.create(ignore=400)


def update_dataset_stats(dataset):
    search = Search(index=get_tweets_index_name(dataset.meta.id))
    search = search.query('term', dataset_id=dataset.meta.id)[0:0]
    search.aggs.metric('created_at_min', 'min', field='created_at')
    search.aggs.metric('created_at_max', 'max', field='created_at')
    search_response = search.execute()
    dataset.first_tweet_created_at = datetime.utcfromtimestamp(
        search_response.aggregations.created_at_min.value / 1000.0)
    dataset.last_tweet_created_at = datetime.utcfromtimestamp(
        search_response.aggregations.created_at_max.value / 1000.0)
    dataset.tweet_count = search_response.hits.total
    dataset.save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser('tweetset_loader')
    parser.add_argument('--debug', action='store_true')

    # Subparsers
    subparsers = parser.add_subparsers(dest='command', help='command help')

    create_parser = subparsers.add_parser('create', help='create a dataset')
    create_parser.add_argument('--path', help='path of dataset', default='/dataset')
    create_parser.add_argument('--filename', help='filename of dataset file', default='dataset.json')
    create_parser.add_argument('--shards', type=int, help='number of shards for this dataset')

    update_parser = subparsers.add_parser('update', help='update dataset metadata')
    update_parser.add_argument('dataset_identifier', help='identifier (a UUID) for the dataset')
    update_parser.add_argument('--path', help='path of dataset', default='/dataset')
    update_parser.add_argument('--filename', help='filename of dataset file', default='dataset.json')
    update_parser.add_argument('--stats', action='store_true', help='Also update dataset statistics')
    update_parser.add_argument('--create', action='store_true', help='Create if does not exist.')

    delete_parser = subparsers.add_parser('delete', help='delete dataset and tweets')
    delete_parser.add_argument('dataset_identifier', help='identifier (a UUID) for the dataset')

    truncate_parser = subparsers.add_parser('truncate', help='delete tweets for a dataset')
    truncate_parser.add_argument('dataset_identifier', help='identifier (a UUID) for the dataset')

    tweets_parser = subparsers.add_parser('tweets', help='add tweets to a dataset')
    tweets_parser.add_argument('dataset_identifier', help='identifier (a UUID) for the dataset')
    tweets_parser.add_argument('--path', help='path of the directory containing the tweet files', default='/dataset')
    tweets_parser.add_argument('--limit', type=int, help='limit the number of tweets to load')
    tweets_parser.add_argument('--skip-count', action='store_true', help='skip count the tweets')
    tweets_parser.add_argument('--store-tweet', action='store_true', help='store the entire tweet')
    tweets_parser.add_argument('--replicas', type=int, default='1', help='number of replicas to make of this dataset')

    dataset_parser = subparsers.add_parser('dataset', help='create a dataset and add tweets')
    dataset_parser.add_argument('--path', help='path of dataset', default='/dataset')
    dataset_parser.add_argument('--filename', help='filename of dataset file', default='dataset.json')
    dataset_parser.add_argument('--limit', type=int, help='limit the number of tweets to load')
    dataset_parser.add_argument('--skip-count', action='store_true', help='skip count the tweets')
    dataset_parser.add_argument('--store-tweet', action='store_true', help='store the entire tweet')
    dataset_parser.add_argument('--shards', type=int, help='number of shards for this dataset')
    dataset_parser.add_argument('--replicas', type=int, default='1', help='number of replicas to make of this dataset')

    subparsers.add_parser('clear', help='delete all indexes')

    args = parser.parse_args()

    # Logging
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('elasticsearch').setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO
    )

    if not args.command:
        parser.print_help()
        quit()

    # Create indexs if they doesn't exist
    dataset_index = DatasetIndex()
    dataset_index.create(ignore=400)

    dataset_id = None
    if args.command in ('create', 'dataset'):
        dataset = to_dataset(read_json(os.path.join(args.path, args.filename)),
                             dataset_id=short_uid(6,
                                                  exists_func=lambda uid: DatasetDocType.get(uid,
                                                                                             ignore=404) is not None))
        dataset.save()
        dataset_id = dataset.meta.id
        log.info('Created {}'.format(dataset_id))
        print('Dataset id is {}'.format(dataset_id))
    if args.command == 'create' and args.shards:
        create_tweet_index(dataset_id, args.shards)
    if args.command == 'update':
        dataset = None
        if not args.create:
            try:
                dataset = DatasetDocType.get(args.dataset_identifier)
            except NotFoundError:
                raise Exception('{} not found'.format(args.dataset_identifier))
        updated_dataset = to_dataset(read_json(os.path.join(args.path, args.filename)), dataset=dataset,
                                     dataset_id=args.dataset_identifier)
        updated_dataset.save()
        if args.stats:
            update_dataset_stats(updated_dataset)
        log.info('Updated {}'.format(updated_dataset.meta.id))
    if args.command == 'delete':
        dataset = DatasetDocType.get(args.dataset_identifier)
        if not dataset:
            raise Exception('{} not found'.format(args.dataset_identifier))
        dataset.delete()
        log.info('Deleted {}'.format(dataset.meta.id))
        delete_tweet_index(args.dataset_identifier)
    if args.command == 'truncate':
        delete_tweet_index(args.dataset_identifier)
    if args.command in ('tweets', 'dataset'):
        if dataset_id is None:
            dataset_id = args.dataset_identifier
        store_tweet = os.environ.get('STORE_TWEET', 'false').lower() == 'true' or args.store_tweet
        if store_tweet:
            log.info('Storing tweet')
        dataset = DatasetDocType.get(dataset_id)
        if not dataset:
            raise Exception('{} not found'.format(dataset_id))
        filepaths = find_files(args.path)
        file_count = count_files(*filepaths)
        tweet_count = 0
        if not args.skip_count:
            log.info('Counting tweets in %s files.', file_count)
            tweet_count = count_lines(*filepaths)
            log.info('{:,} total tweets'.format(tweet_count))

        # Create the index
        # In testing, 500K tweets (storing tweet) = 615MB
        # Thus, 32.5 million tweets per shard to have a max shard size of 40GB
        # In testing, 500k tweets (not storing tweet) = 145MB
        # Thus, 138 million tweets per shard to have a max shard size of 40GB
        tweets_per_shard = 32500000 if store_tweet else 138000000
        shards = (args.shards if hasattr(args, 'shards') else None) or math.ceil(
            float(tweet_count) / tweets_per_shard) or 1
        log.info('Using %s shards and %s replicas for index.', shards, args.replicas)
        create_tweet_index(dataset_id, shards, args.replicas)

        # Doing this in chunks so that can retry if error
        connection = connections.get_connection()
        for chunk in _chunker(to_tweet(tweet_json, dataset_id, store_tweet=store_tweet).to_dict(include_meta=True) for
                              tweet_json in
                              tweet_iter(*filepaths, limit=args.limit, total_tweets=tweet_count)):
            try_count = 1
            while True:
                try:
                    helpers.bulk(connection, chunk)
                    break
                except ConnectionError as e:
                    if try_count == CONNECTION_ERROR_TRIES:
                        raise e
                    log.warning('Sleeping %s after connection error %s of %s: %s', CONNECTION_ERROR_SLEEP, try_count,
                                CONNECTION_ERROR_TRIES, e)
                    try_count += 1
                    sleep(CONNECTION_ERROR_SLEEP)
                    connection = connections.get_connection()

        # Get number of tweets in dataset and update
        sleep(5)
        update_dataset_stats(dataset)
    if args.command == 'clear':
        search = DatasetDocType.search()
        for dataset in search.execute():
            delete_tweet_index(dataset.meta.id)
        dataset_index.delete()
        log.info("Deleted indexes")
