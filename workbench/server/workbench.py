
"""Workbench: Open Source Security Framework """

# Okay this monkey patch call needs to be first
# Note: The thread=False parameters appears to be needed
#       for MongoDB, if you know a better way please do PR :)
from gevent import monkey; monkey.patch_all(thread=False) # Monkey!
from gevent import signal as gevent_signal
import signal
import sys, os
import zerorpc
import zmq
import logging
logging.basicConfig()
import StringIO
import json
import hashlib
import inspect
import funcsigs
import ConfigParser
import magic

# Workbench server imports
try:
    from . import data_store
    from . import els_indexer
    from . import neo_db
    from . import plugin_manager
    from bro import bro_log_reader

# Okay this happens when you're running workbench in a debugger so having
# this is super handy and we'll keep it even though it hurts coverage score.
except ValueError:
    import data_store
    import els_indexer
    import neo_db
    import plugin_manager
    from bro import bro_log_reader


class WorkBench(object):
    """ Workbench: Open Source Security Framework. """

    def __init__(self, store_args=None, els_hosts=None, neo_uri=None):
        """Initialize the Framework.

        Args:
            store_args: Dictionary with keys uri,database,samples_cap, worker_cap.
            els_hosts: The address where Elastic Search Indexer is running.
            neo_uri: The address where Neo4j is running.
        """
        # Announce Version
        try:
            self.version = sys.modules['workbench'].__version__
        except (AttributeError, KeyError):
            self.version = 'unknown'
        print '<<< Workbench Version %s >>>' % self.version

        # Open DataStore
        self.data_store = data_store.DataStore(**store_args)

        # ELS Indexer
        try:
            self.indexer = els_indexer.ELSIndexer(**{'hosts': els_hosts} if els_hosts else {})
        except SystemExit:
            print 'Could not connect to ELS. Is it running?'
            self.indexer = els_indexer.ELSStubIndexer(**{'uri': neo_uri} if neo_uri else {})

        # Neo4j DB
        try:
            self.neo_db = neo_db.NeoDB(**{'uri': neo_uri} if neo_uri else {})
        except RuntimeError:
            print 'Could not connect to Neo4j DB. Is it running?  $ neo4j start'
            self.neo_db = neo_db.NeoDBStub(**{'uri': neo_uri} if neo_uri else {})

        # Create Plugin Manager
        self.plugin_meta = {}
        plugin_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)),'../workers')
        self.plugin_manager = plugin_manager.PluginManager(self._new_plugin, plugin_dir=plugin_dir)

        # Store information about commands and workbench
        self._store_information()


    #######################
    # Sample Methods
    #######################
    def store_sample(self, filename, input_bytes, type_tag):
        """ Store a sample into the DataStore.
            Args:
                filename: name of the file (used purely as meta data not for lookup)
                input_bytes: the actual bytes of the sample e.g. f.read()
                type_tag: ('exe','pcap','pdf','json','swf', or ...)
            Returns:
                the md5 of the sample.
        """

        # If the sample comes in with an unknown type_tag try to determine it
        if type_tag == 'unknown':
            print '<<< Unknown File: Trying to Determine Type >>>'
            type_tag = self.guess_type_tag(input_bytes)

        return self.data_store.store_sample(filename, input_bytes, type_tag)

    def get_sample(self, md5):
        """ Get a sample from the DataStore.
            Args:
                md5: the md5 of the sample
            Returns:
                A dictionary of meta data about the sample which includes
                a ['raw_bytes'] key that contains the raw bytes.
        """
        sample = self.data_store.get_sample(md5)
        return {'sample': sample}

    def get_sample_window(self, type_tag, size):
        """ Get a sample from the DataStore.
            Args:
                type_tag: the type of samples ('pcap','exe','pdf')
                size: the size of the window in MegaBytes (10 = 10MB)
            Returns:
                A list of md5s representing the newest samples within the size window
        """
        return self.data_store.get_sample_window(type_tag, size)

    def has_sample(self, md5):
        """ Do we have this sample in the DataStore.
            Args:
                md5: the md5 of the sample
            Returns:
                True or False
        """
        return self.data_store.has_sample(md5)

    def list_samples(self, predicate={}):
        """List all samples that meet the predicate or all if predicate is not specified.

        Args:
            predicate: Match samples against this predicate (or all if not specified)

        Returns:
            List of dictionaries with matching samples {'md5':md5, 'filename': 'foo.exe', 'type_tag': 'exe'}
        """
        return self.data_store.list_samples(predicate)

    @zerorpc.stream
    def stream_sample(self, md5, max_rows):
        """ Stream the sample by giving back a generator, typically used on 'logs'.
            Args:
                md5: the md5 of the sample
                max_rows: the maximum number of rows to return (None for all)
            Returns:
                A generator that yields rows of the file/log
        """

        # Grab the sample and it's raw bytes
        sample = self.data_store.get_sample(md5)
        raw_bytes = sample['raw_bytes']

        # Figure out the type of file to be streamed
        type_tag = sample['type_tag']
        if type_tag == 'bro':
            bro_log = bro_log_reader.BroLogReader(convert_datetimes=False)
            mem_file = StringIO.StringIO(raw_bytes)
            generator = bro_log.read_log(mem_file)
            return generator
        elif type_tag == 'els_query':
            els_log = json.loads(raw_bytes)
            # Try to determine a couple of different types of ELS query results
            if 'fields' in els_log['hits']['hits'][0]:
                generator = (row['fields'] for row in els_log['hits']['hits'][:max_rows])
            else:
                generator = (row['_source'] for row in els_log['hits']['hits'][:max_rows])
            return generator
        elif type_tag == 'log':
            generator = ({'row':row} for row in raw_bytes.split('\n')[:max_rows])
            return generator
        elif type_tag == 'json':
            generator = (row for row in json.loads(raw_bytes)[:max_rows])
            return generator
        else:
            raise RuntimeError('Cannot stream file %s with type_tag:%s' % (md5, type_tag))

    def guess_type_tag(self, input_bytes):
        """ Try to guess the type_tag for this sample """
        mime_to_type = {'application/x-dosexec': 'exe',
                        'application/pdf': 'pdf',
                        'application/zip': 'zip',
                        'application/jar': 'jar',
                        'application/vnd.ms-cab-compressed': 'cab',
                        'text/plain': 'txt',
                        'image/gif': 'gif',
                        'image/jpeg': 'jpg',
                        'image/png': 'png',
                        'text/html': 'html',
                        'application/vnd.ms-fontobject': 'ms_font',
                        'application/x-shockwave-flash': 'swf'}

        # See what filemagic can determine
        with magic.Magic(flags=magic.MAGIC_MIME_TYPE) as mag:
            mime_type = mag.id_buffer(input_bytes[:1024])
            if mime_type in mime_to_type:
                return mime_to_type[mime_type]
            else:
                print '<<< Sample Type could not be Determined >>>'
                return 'unknown'



    #######################
    # Index Methods
    #######################
    def index_sample(self, md5, index_name):
        """ Index a stored sample with the Indexer.
            Args:
                md5: the md5 of the sample
                index_name: the name of the index
            Returns:
                Nothing
        """
        generator = self.stream_sample(md5, None)
        for row in generator:
            self.indexer.index_data(row, index_name)

    def index_worker_output(self, worker_name, md5, index_name, subfield):
        """ Index worker output with the Indexer.
            Args:
                worker_name: 'strings', 'pe_features', whatever
                md5: the md5 of the sample
                index_name: the name of the index
                subfield: index just this subfield (None for all)
            Returns:
                Nothing
        """

        # Grab the data
        if subfield:
            data = self.work_request(worker_name, md5)[worker_name][subfield]
        else:
            data = self.work_request(worker_name, md5)[worker_name]

        # Okay now index the data
        self.indexer.index_data(data, index_name=index_name, doc_type='unknown')

    def search(self, index_name, query):
        """ Search a particular index in the Indexer
            Args:
                index_name: the name of the index
                query: the query against the index
            Returns:
                All matches to the query
        """
        return self.indexer.search(index_name, query)


    #######################
    # Graph Methods
    #######################
    def add_node(self, node_id, name, labels):
        """ Add a node to the graph with name and labels.
            Args:
                node_id: the unique node_id e.g. 'www.evil4u.com'
                name: the display name of the node e.g. 'evil4u'
                labels: a list of labels e.g. ['domain','evil']
            Returns:
                Nothing
        """
        self.neo_db.add_node(node_id, name, labels)

    def has_node(self, node_id):
        """ Does the Graph DB have this node
            Args:
                node_id: the unique node_id e.g. 'www.evil4u.com'
            Returns:
                True/False
        """
        return self.neo_db.has_node(node_id)

    def add_rel(self, source_id, target_id, rel):
        """ Add a relationship: source, target must already exist (see add_node)
            'rel' is the name of the relationship 'contains' or whatever.
            Args:
                source_id: the unique node_id of the source
                target_id: the unique node_id of the target
                rel: name of the relationship
            Returns:
                Nothing
        """
        self.neo_db.add_rel(source_id, target_id, rel)

    def clear_graph_db(self):
        """ Clear the Graph Database of all nodes and edges.
            Args:
                None
            Returns:
                Nothing
        """
        self.neo_db.clear_db()

    def clear_db(self):
        """ Clear the Main Database of all samples and worker output.
            Args:
                None
            Returns:
                Nothing
        """
        self.data_store.clear_db()

        # Store information about commands and workbench
        self._store_information()        


    #######################
    # Work Request Methods
    #######################
    def work_request(self, worker_name, md5, subkeys=None):
        """ Make a work request for an existing stored sample.
            Args:
                worker_name: 'strings', 'pe_features', whatever
                md5: the md5 of the sample
                subkeys: just return a subfield e.g. 'foo' or 'foo.bar' (None for all) 
            Returns:
                The output of the worker or just the subfield of the worker output
        """

        # Check valid
        if worker_name not in self.plugin_meta.keys():
            raise RuntimeError('Invalid work request for class %s (not found)' % (worker_name))

        # Get results (even if we have to wait for them)
        # Note: Yes, we're going to wait. Gevent concurrent execution will mean this
        #       code gets spawned off and new requests can be handled without issue.
        work_results = self._recursive_work_resolver(worker_name, md5)

        # Subkeys? (Fixme this is super klutzy)
        if subkeys:
            try:
                sub_results = {}
                for subkey in subkeys:
                    tmp = work_results[worker_name]
                    for key in subkey.split('.'):
                        tmp = tmp[key]
                        sub_results[key] = tmp
                work_results = sub_results
            except (KeyError, TypeError):
                raise RuntimeError('Could not get one or more subkeys for: %s' % (work_results))

        # Clean it and ship it
        work_results = self.data_store.clean_for_serialization(work_results)
        return work_results

    @zerorpc.stream
    def batch_work_request(self, worker_name, kwargs={}):
        """Make a batch work request for an existing set of stored samples.

        A subset of sample can be specified with kwargs.

        Args:
            worker_name: 'strings', 'pe_features', whatever
            kwargs: a way of specifying subsets of samples ({} for all)
                type_tag: subset based on sample type (e.g. type_tag='exe')
                md5_list: subset just the samples in this list
                subkeys: return just this subkey (e.g. 'foo' or 'foo.bar')
        Returns:
            A generator that yields rows of worker output or subfields of the worker output
        """
        type_tag = kwargs.get('type_tag',None)
        md5_list = kwargs.get('md5_list',None)
        subkeys = kwargs.get('subkeys',None)

        # If no md5_list specified put all samples (of type type_tag if not None)
        if not md5_list:
            md5_list = self.data_store.all_sample_md5s(type_tag) 

        # Loop through all the md5s and return a generator with yield
        for md5 in md5_list:
            try:
                if subkeys:
                    yield self.work_request(worker_name, md5, subkeys)
                else:
                    yield self.work_request(worker_name, md5)[worker_name]
            except KeyError:
                continue

    def store_sample_set(self, md5_list):
        """ Store a sample set (which is just a list of md5s).

            Note: All md5s must already be in the data store.

            Args:
                md5_list: a list of the md5s in this set (all must exist in data store)

            Returns:
                The md5 of the set (the actual md5 of the set
        """
        for md5 in md5_list:
            if not self.has_sample(md5):
                raise RuntimeError('Sample not found all items in sample_set\
                                    must be in the datastore: %s (not found)' % (md5))
        set_md5 = hashlib.md5(str(md5_list)).hexdigest()
        self._store_work_results({'md5_list':md5_list}, 'sample_set', set_md5)
        return set_md5

    def get_sample_set(self, md5):
        """ Store a sample set (which is just a list of md5s).

            Args:
                md5_list: a list of the md5s in this set (all must exist in data store)

            Returns:
                The md5 of the set (the actual md5 of the set
        """
        return self._get_work_results('sample_set', md5)

    @zerorpc.stream
    def stream_sample_set(self, md5):
        """ Stream a sample set (which is just a list of md5s).

            Args:
                md5: the md5 of the sample_set

            Returns:
                A generator that yields the md5s in the sample set
        """
        for md5 in self._get_work_results('sample_set', md5)['md5_list']:
            yield md5

    def get_datastore_uri(self):
        """ Gives you the current datastore URL.

            Args:
                None

            Returns:
                The URI of the data store currently being used by Workbench
        """
        return self.data_store.get_uri()


    ##################
    # Help
    ##################        
    def help(self, topic=None):
        """ Returns the formatted, colored help """
        if not topic:
            topic = 'workbench'
        return self.work_request('help_cli', topic)['help_cli']['help']

    # Fixme: These are internal methods that basically just provide help text
    def _help_workbench(self):
        """ Help on Workbench """
        help = 'Welcome to Workbench Help:'
        help += '\n\t- workbench.help(topic) -- where topic can be a help, command or worker'
        help += '\n\t- workbench.help(\'basic\') -- for getting started help'
        help += '\n\t- workbench.help(\'commands\') -- for help on workbench commands'
        help += '\n\t- workbench.help(\'workers\') -- for help on available workers'
        help += '\n\nSee http://github.com/SuperCowPowers/workbench for more information\n'
        return help

    def _help_basic(self):
        """ Help for Workbench Basics """
        help =  'Workbench: Getting started...'
        help += '\n\t - 1) $ workbench.help(\'commands\') -- for help on workbench commands'
        help += '\n\t - 2) $ workbench.help(\'store_sample\') -- for help on storing a new sample'
        help += '\n\t - 3) $ workbench.help(\'workers\') -- for help on available workers'
        help += '\n\t - 4) $ workbench.help(\'meta\') -- for help on the meta worker'
        help += '\n\t - 5) $ workbench.store_sample(\'evil.exe\', raw_bytes, \'exe\')'
        help += '\n\t - 6) $ workbench.work_request(\'meta\', md5)'
        return help

    def _help_commands(self):
        """ Help on all the available commands """
        help =  'Workbench Commands:'
        for command in self.list_all_commands():
            help += '\n\t%s' % self.work_request('help_cli', command)
        return help


    ##################
    # Introspection
    ##################
    def list_all_commands(self):
        """ Returns a list of all the Workbench commands"""
        commands = [name for name, _ in inspect.getmembers(self, predicate=inspect.ismethod) if not name.startswith('_')]
        # commands.append('batch_work_request') # I think the zerorpc decorator messes up inspect
        return commands

    def list_all_workers(self):
        """ List all the currently loaded workers """
        return self.plugin_meta.keys()

    def info(self, component):
        """ Get the information about this component """

        # Grab it, clean it and ship it
        work_results = self._get_work_results('info', component)
        return self.data_store.clean_for_serialization(work_results)

    def store_info(self, info_dict, component, type_tag):
        """ Store information about a component. The component could be a
            worker or a commands or a class, or whatever you want, the
            only thing to be aware of is name collisions. """

        # Enforce dictionary input
        if not isinstance(info_dict, dict):
            print 'Critical: info_dict must be a python dictionary, got %s' % type(info_dict)
            return

        # Ensure values are not functions/methods/classes
        info_storage = {key:value for key, value in info_dict.iteritems() if not hasattr(value, '__call__')}

        # Place the type_tag on it and store it
        info_storage['type_tag'] = type_tag
        self._store_work_results(info_storage, 'info', component)

    ##################
    # Testing
    ##################
    def test_worker(self, worker_name):
        """ Run the test for a specific worker """

        # First find the plugin
        try:
            plugin = self.plugin_meta[worker_name]
        except KeyError:
            return '%s worker not found.. misspelled?' % worker_name

        # Now try to run the test
        try:
            return plugin['test']()
        except (AttributeError, KeyError) as error:
            output = 'Failure for plugin: %s' % (worker_name)
            output += 'Error: %s' % error
            return output


    ####################
    # Internal Methods
    ####################
    def _store_information(self):
        """ Store infomation about Workbench and its commands """
        
        print '<<< Generating Information Storage >>>'

        """ Stores information on Workbench commands and signatures """
        for name, meth in inspect.getmembers(self, predicate=inspect.ismethod):
            if not name.startswith('_'):
                info = {'command': name, 'sig': str(funcsigs.signature(meth)), 'docstring': meth.__doc__}
                self.store_info(info, name, type_tag='command')

        """ Stores help text into the workbench information system """
        self.store_info({'help': '<<< Workbench Version %s >>>' % self.version}, 'version', type_tag='help')
        self.store_info({'help': self._help_workbench()}, 'workbench', type_tag='help')
        self.store_info({'help': self._help_basic()}, 'basic', type_tag='help')
        self.store_info({'help': self._help_commands()}, 'commands', type_tag='help')

    def _new_plugin(self, plugin):
        """ Internal: This method handles the mechanics around new plugins. """

        # First store the plugin info into our data store
        self.store_info(plugin, plugin['name'], type_tag='worker')

        # Place it into our active plugin list
        self.plugin_meta[plugin['name']] = plugin

    def _store_work_results(self, results, collection, md5):
        """ Internal: Stores the work results of a worker."""
        self.data_store.store_work_results(results, collection, md5)
    def _get_work_results(self, collection, md5):
        """ Internal: Method for fetching work results."""
        results = self.data_store.get_work_results(collection, md5)
        return {collection: results} if results else None


    # So the trick here is that since each worker just stores it's input dependencies
    # we can resursively backtrack and all the needed work gets done.
    def _recursive_work_resolver(self, worker_name, md5):
        """ Internal: Input dependencies are resursively backtracked, invoked and then
               passed down the pipeline until htting the requested worker. """

        # Looking for the sample or sample_set?
        if worker_name == 'sample':
            # If we have a sample set with this md5, return it
            if self.get_sample_set(md5):
                return self.get_sample_set(md5)
            # Return the sample (might raise a RuntimeError)
            return self.get_sample(md5)

        # Looking for info?
        if worker_name == 'info':
            return self._get_work_results('info', md5)

        # Do I actually have this plugin? (might have failed, etc)
        if (worker_name not in self.plugin_meta):
            print 'Request for non-existing or failed plugin: %s' % (worker_name)
            return {}

        # If the results exist and the mod_time is newer than the plugin's, I'm done
        collection = self.plugin_meta[worker_name]['name']
        work_results = self._get_work_results(collection, md5)
        if work_results:
            if self.plugin_meta[worker_name]['mod_time'] < work_results[collection]['__time_stamp']:
                return work_results
            else:
                print 'Updating work results for new plugin: %s' % (worker_name)

        # Okay either need to generate (or re-generate) the work results
        dependencies = self.plugin_meta[worker_name]['dependencies']
        dependant_results = {}
        for dependency in dependencies:
            dependant_results.update(self._recursive_work_resolver(dependency, md5))
        print 'New work for plugin: %s' % (worker_name)
        work_results = self.plugin_meta[worker_name]['class']().execute(dependant_results)

        # Enforce dictionary output
        if not isinstance(work_results, dict):
            print 'Critical: Plugin %s MUST produce a python dictionary!' % worker_name
            return None

        # Store the results and return
        self._store_work_results(work_results, collection, md5)
        return self._get_work_results(collection, md5)

    def _find_element(self,d,k):
        if k in d: return d[k]
        submatch = [d[_k][k] for _k in d if k in d[_k]]
        return submatch[0] if submatch else None


def run():
    """ Run the workbench server """

    # Load the configuration file relative to this script location
    config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.ini')
    workbench_conf = ConfigParser.ConfigParser()
    config_ini = workbench_conf.read(config_path)
    if not config_ini:
        print 'Could not locate config.ini file, tried %s : exiting...' % config_path
        exit(1)

    # Pull configuration settings
    datastore_uri = workbench_conf.get('workbench', 'datastore_uri')
    database = workbench_conf.get('workbench', 'database')
    worker_cap = workbench_conf.getint('workbench', 'worker_cap')
    samples_cap = workbench_conf.getint('workbench', 'samples_cap')

    # Spin up Workbench ZeroRPC
    try:
        store_args = {'uri': datastore_uri, 'database': database, 'worker_cap':worker_cap, 'samples_cap':samples_cap}
        print '<<< Workbench Server at %s >>>' % ('tcp://0.0.0.0:4242')
        workbench = zerorpc.Server(WorkBench(store_args=store_args), name='workbench', heartbeat=60)
        workbench.bind('tcp://0.0.0.0:4242')
        print '\nWorkbench is ready and feeling super duper!'
        gevent_signal(signal.SIGTERM, workbench.stop)
        gevent_signal(signal.SIGINT, workbench.stop)
        gevent_signal(signal.SIGKILL, workbench.stop)
        workbench.run()
        print '\nWorkbench Server Shutting Down... and dreaming of sheep...'

    except zmq.error.ZMQError:
        print '\nInfo: Could not start Workbench server (no worries, probably already running...)\n'


# Test that just calls main
def test():
    run()

if __name__ == '__main__':
    run()
