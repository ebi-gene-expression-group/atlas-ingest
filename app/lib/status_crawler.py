'''
crawls fg log files for status of experiments in system

crawl=True diggs deep into all files opening al metadata files to extract information (approx several min execution)
crawl=False fast lookup for accessions and status of those accessions (under 1 sec execution)
'''
__author__ = "hewgreen"
__license__ = "Apache 2.0"
__date__ = "15/07/2019"


'''
Profiling

Top 3 modules by % runtime:

idf_sdrf_metadata_scraper 69%
get_latest_idf_sdrf 20%
get_file_modified_date 8.3%

idf_sdrf_metadata_scraper: opens and reads files therefore takes some time. Other tweeks were not faster.
get_latest_idf_sdrf: not looked at improving this yet
get_file_modified_date: This could be used to checkup against last pickled run output to avoid opening files that were already read if speed becomes a blocker
'''


import json
import os
import pandas as pd
import re
import glob
from datetime import datetime
import csv
from tqdm import tqdm
import pickle
import time
import mysql.connector
import psycopg2
from app.lib.google_API import google_sheet_output
import sys
from collections import defaultdict
from collections import OrderedDict

class atlas_status:

    def timeit(method):
        def timed(*args, **kw):
            ts = time.time()
            result = method(*args, **kw)
            te = time.time()
            if 'log_time' in kw:
                name = kw.get('log_name', method.__name__.upper())
                kw['log_time'][name] = int((te - ts) * 1000)
            else:
                print('Completed func {} in {} ms'.format(method.__name__, (te - ts) * 1000))
            return result
        return timed

    @timeit
    def __init__(self, sources_config, db_config, google_client_secret=None, google_output=True, spreadsheetname="DEV Ingest Status", crawl=True, verbose=True):

        # configuration
        self.verboseprint = print if verbose else lambda *a, **k: None
        self.google_client_secret = google_client_secret
        self.spreadsheetname = spreadsheetname
        with open(sources_config) as f:
            self.sources_config = json.load(f)
        with open(db_config) as d:
            self.db_config = json.load(d)

        # status tracking
        self.status_types = self.get_status_types()
        self.status_type_order = ['external', 'incoming', 'loading', 'analysing', 'processed', 'published']
        assert set(self.status_types) == set(
            self.status_type_order), 'Unrecognised status in config. Please update status type order list.'
        self.timestamp = datetime.fromtimestamp(datetime.now().timestamp()).isoformat()
        self.verboseprint('Initialised {}'.format(self.timestamp))

        # accession search
        self.found_accessions = self.accession_search() # scans dir in config to find '*.idf.txt' or accession directories
        self.accession_final_status = self.status_tracker() # determines status of each dataset based on location of files
        self.get_min_max_status() # sets two variables with min and max status based on status_type_order

        if crawl: # metadata crawling opening idfs/sdrfs and dbs

            # idf/sdrf/curator_file crawl
            self.secondary_accession_mapper() # extracts secondary accessions from idf making a map dict and a complete set (all_secondary_accessions) for duplication checks
            self.extracted_metadata = self.idf_sdrf_metadata_scraper()
            self.curators_by_acession = self.lookup_curator_file()

            # other
            self.get_latest_idf_sdrf()  # finds path to latest idf and sdrf file
            self.mod_time = self.get_file_modified_date()

            # metadata from sys DBs
            self.atlas_eligibility_by_accession = self.get_atlas_eligibility_status()

        # output
        if google_output:
            output_dfs = self.df_compiler() # this function should be edited to change the information exported to the google sheets output
            google_sheet_output(self, output_dfs, self.spreadsheetname) # table exported to https://docs.google.com/spreadsheets/d/13gxKodyl-zJTeyCxXtxdw_rp60WJHMcHLtZhxhg5opo/edit#gid=0

        self.pickle_out()

    @timeit
    def get_status_types(self):
        status_types = set()
        for x, y in self.sources_config.items():
            for n in y['stage']:
                status_types.add(n)
        return list(status_types)

    @timeit
    def accession_search(self):
        self.verboseprint('Performing accession search {}'.format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        self.all_primary_accessions = set()
        found_accessions = {}
        counter = 0
        for path, info in self.sources_config.items():
            counter += 1
            self.verboseprint('Searching path {} {}/{}'.format(path, counter, len(self.sources_config)))

            pre_accessions = os.listdir(path)
            self.accession_regex = re.compile('^E-(GEOD|MTAB|PROT|ENAD|AFMX|CURD|EHCA|MEXP|TABM|NASC|ERAD|GEUV|JJRD|ATMX|HCAD|MIMR|CBIL|MAXD)-[0-9]+$')
            for pre_accession in pre_accessions:
                if not pre_accession.endswith('.merged.idf.txt'):
                    accession = pre_accession.strip('.idf.txt')
                    if self.accession_regex.match(accession):
                        self.all_primary_accessions.add(accession)
                        found_accessions[(path, accession)] = dict(
                                                                        # config_path=path,
                                                                        accession=accession,
                                                                        tech=info.get('tech', None),
                                                                        stage=info.get('stage', None),
                                                                        resource=info.get('resource', None),
                                                                        source=info.get('source', None)
                        )
        self.verboseprint('Found {} accessions in {} directories'.format(len(found_accessions), len(self.sources_config)))
        return found_accessions

    @timeit
    def status_tracker(self):
        self.verboseprint('Calculating status of each project {}'.format(datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        accession_status = {}
        accession_status_counter = {}
        for key, value in self.found_accessions.items():
            accession = value['accession']
            stage = value['stage']

            index = self.status_type_order.index(stage[-1])
            if accession not in accession_status_counter:
                accession_status_counter[accession] = index
                accession_status[accession] = ' '.join(stage)
            elif index > accession_status_counter.get(accession):
                accession_status_counter[accession] = index
                accession_status[accession] = ' '.join(stage)

        return accession_status

    @timeit
    def get_min_max_status(self):
        # Some paths define multiple statuses. This narrows it to the latter most status according to status_type_order
        self.accession_min_status = {}
        self.accession_max_status = {}
        for accession, status in self.accession_final_status.items():
            self.accession_min_status[accession] = self.status_type_order[min([self.status_type_order.index(x) for x in status.split(' ')])]
            self.accession_max_status[accession] = self.status_type_order[max([self.status_type_order.index(x) for x in status.split(' ')])]

    @timeit
    def secondary_accession_mapper(self):
        self.verboseprint('Finding secondary accessions {}'.format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        idf_files = []
        for path, values in tqdm(self.sources_config.items(), unit='Paths in config'):
            self.verboseprint('Secondary accession search in {}'.format(path))
            f = [f for f in glob.glob(path + "**/*.idf.txt", recursive=True)]
            idf_files += f

        self.all_secondary_accessions = set()
        self.secondary_accessions_mapping = {}
        for idf_file in tqdm(idf_files, unit='IDF files'):
            accession = idf_file.split('/')[-1].strip('.idf.txt')
            with open (idf_file, "r") as f:
                try:
                    contents = f.read()
                except UnicodeDecodeError:
                    self.verboseprint('Cannot open {}'.format(idf_file))
                search = re.findall(r'Comment \[SecondaryAccession\](.*?)\n', contents)
                if search:
                    secondary_accessions = [x.strip('\t') for x in search]
                    if len(secondary_accessions) == 1 and len(secondary_accessions[0]) == 0:
                        continue
                    else:
                        self.secondary_accessions_mapping[accession] = secondary_accessions
                        self.all_secondary_accessions.update(secondary_accessions)

    @timeit
    def get_latest_idf_sdrf(self):
        self.verboseprint('Getting latest IDF and SDRF paths {}'.format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))

        def list_converter(file_list):
            files_found = {}
            for filepath in file_list:
                accession = filepath.split('/')[-1].replace('.idf.txt', '').replace('.sdrf.txt', '')
                if accession in files_found:
                    files_found[accession].append(filepath)
                else:
                    files_found[accession] = [filepath]
            return files_found

        def get_ranked_paths():
            # ranks path by status type, used to estimate out where latest metadata is.
            preordered_paths = []
            latest_stage_ranks = []
            for path, metadata in self.sources_config.items():
                preordered_paths.append(path)
                latest_stage_ranks.append(max([self.status_type_order.index(n) for n in metadata.get('stage')]))
            return [x for _,x in sorted(zip(latest_stage_ranks,preordered_paths))]

        def get_latter_ranked_path(paths_by_accession, ranked_paths):
            ranked_paths_by_accession = {}
            for accession, path_list in paths_by_accession.items():
                if len(path_list) == 1:
                    latest_path = path_list[0]
                else:
                    try:
                        trunc_paths = [v.split('/E-')[0] for v in path_list]  # remove accession specific endings
                        path_ranks = [ranked_paths.index(n) for n in trunc_paths]
                        latest_path = path_list[path_ranks.index(max(path_ranks))]  # get idf/sdrf from latter loc
                    except ValueError:
                        continue #todo this needs more investigation in prod got ValueError: '/nfs/production3/ma/home/atlas3-production/singlecell/experiment/ng' is not in list
                ranked_paths_by_accession[accession] = latest_path
            return ranked_paths_by_accession



        idf_list_ = []
        sdrf_list_ = []
        for path, metadata in tqdm(self.sources_config.items(), unit='Paths in config'):
            self.verboseprint('IDF/SDRF path finder exploring {}'.format(path))
            idf_list_ += glob.glob(path + '/*/*.idf.txt') + glob.glob(path + '/*.idf.txt')
            sdrf_list_ += glob.glob(path + '/*/*.sdrf.txt') + glob.glob(path + '/*.sdrf.txt')

            # attempted optimisation (seems to hang)
            # idf_list_ += glob.glob(path + '/*.idf.txt')
            # sdrf_list_ += glob.glob(path + '/*.sdrf.txt')
            # for item in os.listdir(path):
            #     if self.accession_regex.match(item):
            #         idf_list_ += glob.glob(path + '/*.idf.txt')
            #         sdrf_list_ += glob.glob(path + '/*.sdrf.txt')

        idf_list = [x for x in idf_list_ if self.accession_regex.match(x.split('/')[-1].replace('.idf.txt', '').replace('.sdrf.txt', ''))]
        sdrf_list = [x for x in sdrf_list_ if self.accession_regex.match(x.split('/')[-1].replace('.idf.txt', '').replace('.sdrf.txt', ''))]

        ranked_paths = get_ranked_paths()
        self.idf_path_by_accession = get_latter_ranked_path(list_converter(idf_list), ranked_paths)
        self.sdrf_path_by_accession = get_latter_ranked_path(list_converter(sdrf_list), ranked_paths)

        # get the path where the accession was initially found at
        paths_by_accession = defaultdict(list)
        for k, v in self.found_accessions.items():
            paths_by_accession[v.get('accession')].append(k[0])
        self.path_by_accession = get_latter_ranked_path(paths_by_accession, ranked_paths)

    @timeit
    def idf_sdrf_metadata_scraper(self):
        self.verboseprint('Scraping project metadata {}'.format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        extracted_metadata = {}

        # edit this list to extract different metadata.
        # NB found some column names contain an erroneous space which is accounted for.
        idf_get = {'Experiment Type': ['Comment[EAExperimentType', 'Comment [EAExperimentType'],
                   'Curator': ['Comment[EACurator]', 'Comment [EACurator]'],
                   'Analysis Type': ['Comment[AEExperimentType]', 'Comment [AEExperimentType]']}
        sdrf_get = {'Single-cell Experiment Type' : ['Comment[library construction]', 'Comment [library construction]'],
                    'Organism' : ['Characteristics[organism]', 'Characteristics [organism]', 'Characteristics [Organism]']}
        assert not set(idf_get.keys()).intersection(set(sdrf_get.keys())), 'Keys should be unique in metadata config lists above'


        metadata_get = [{'paths': self.sdrf_path_by_accession, 'get_params' : sdrf_get},
                        {'paths': self.idf_path_by_accession, 'get_params': idf_get}]

        self.unicode_error_accessions = []
        self.unicode_error_paths = []
        for metadata_file_type in metadata_get:
            for accession, metadata_file in metadata_file_type.get('paths').items():
                if metadata_file:
                    try:
                        with open(metadata_file, newline='') as s:
                            reader = csv.DictReader(s, delimiter='\t')
                            for row in reader:
                                for output_colname, input_colname_list in metadata_file_type.get('get_params').items():
                                    for input_colname in input_colname_list:
                                        output_value = row.get(input_colname)
                                        if output_value and output_value != None:
                                            break
                                    if output_colname not in extracted_metadata and output_value != None:
                                        extracted_metadata[output_colname] = {accession: output_value}
                                    elif output_value != None:
                                        extracted_metadata[output_colname].update({accession : output_value})
                    except UnicodeDecodeError:
                        self.unicode_error_accessions.append(accession)
                        self.unicode_error_paths.append(metadata_file)
                        self.verboseprint('Failed to open {} due to UnicodeDecodeError'.format(metadata_file))
                        # todo fix unit decode error affecting some files. They tend to be charset=unknown-8bit
                        continue


        return extracted_metadata

    @timeit
    def get_file_modified_date(self):
        self.verboseprint("Getting datestamp of project's last modification {}".format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        mod_time = {}
        for accession, idf_path in self.idf_path_by_accession.items():
            sdrf_path = self.sdrf_path_by_accession.get(accession)
            if idf_path and sdrf_path:
                idf_mode_time = os.path.getmtime(idf_path)
                sdrf_mode_time = os.path.getmtime(sdrf_path)
                mod_time[accession] = datetime.fromtimestamp(max(idf_mode_time, sdrf_mode_time)).isoformat()
            elif idf_path:
                idf_mode_time = os.path.getmtime(idf_path)
                mod_time[accession] = datetime.fromtimestamp(idf_mode_time).isoformat()
            elif sdrf_path:
                sdrf_mode_time = os.path.getmtime(sdrf_path)
                mod_time[accession] = datetime.fromtimestamp(sdrf_mode_time).isoformat()
        return mod_time

    @timeit
    def pickle_out(self):
        if not os.path.exists('logs'):
            os.makedirs('logs')

        # binary log of results
        filename = 'logs/' + str(self.timestamp) + '.atlas_status.log'
        filehandler = open(filename, 'wb')
        pickle.dump(self, filehandler)
        print('Crawler results dumped to pickle')

        # human readable log
        filename = 'logs/last_run_text.log'
        data = {
            "Primary accessions found": list(self.all_primary_accessions),
            "Failed to open accessions due to unicode error": list(self.unicode_error_accessions),
            "Failed to open file paths due to unicode error": list(self.unicode_error_paths)
        }
        with open(filename, 'w') as filehandler:
            json.dump(data, filehandler)

    def lookup_curator_file(self):
        curator_signature = {}
        for path, info in self.sources_config.items():
            for file in glob.glob(path + '/E-*/.curator.*'):
                curator = file.split('.')[-1]
                accession = file.split('/')[-2]
                curator_signature[accession] = curator
        return curator_signature

    @timeit
    def get_atlas_eligibility_status(self): # this func connects to various atlas db because output is not in logs

        atlas_eligibility_status = {}
        for name, connection_details in self.db_config.items():
            self.verboseprint('\nSearching {} db for atlas eligibility score'.format(name))
            if connection_details['dbtype'] == 'mysql':
                db = mysql.connector.connect(host=connection_details['host'],
                                       user=connection_details['user'],
                                       password=connection_details['password'],
                                       port=connection_details['port'],
                                       database=name)
            elif connection_details['dbtype'] == 'postgres':
                db = psycopg2.connect(host=connection_details['host'],
                                       user=connection_details['user'],
                                       password=connection_details['password'],
                                       port=connection_details['port'],
                                       dbname=name)
            else:
                raise ValueError('DB type {} not interpreted. Review db_config.json.'.format(connection_details['dbtype']))


            cursor = db.cursor()
            query = "SELECT {} FROM {}".format(connection_details['atlas_eligibility_status_columns'], connection_details['atlas_eligibility_status_table'])
            cursor.execute(query)

            for row in tqdm(cursor):
                accession = row[0]
                atlas_fail_score = row[1]
                if atlas_fail_score and accession not in atlas_eligibility_status:
                    atlas_eligibility_status[accession] = atlas_fail_score

        self.verboseprint('\n{} atlas status checks were detected in atlas databases'.format(len(atlas_eligibility_status)))
        pop_list = []
        for accession in atlas_eligibility_status.keys():
            if accession not in self.accession_final_status:
                pop_list.append(accession)
        for accession in pop_list:
            atlas_eligibility_status.pop(accession)
        self.verboseprint('{} of these accessions were detected in source_config locations and these '
                          'were added to the state tracker\n'.format(len(atlas_eligibility_status)))


        return atlas_eligibility_status # remove entries that do not have a status in self.accession_final_status

    @timeit
    def df_compiler(self):
        self.verboseprint('Combining results into summary dataframe {}'.format(
            datetime.fromtimestamp(datetime.now().timestamp()).isoformat()))
        # combine accession keyed dictionaries
        # NB extracted metadata is an extra dict of dicts with various values from metadata scraping
        input_dicts = {"Status": self.accession_final_status,
                       "Discovery Location": self.path_by_accession,
                       "Secondary Accessions": self.secondary_accessions_mapping,
                       "IDF": self.idf_path_by_accession,
                       "SDRF": self.sdrf_path_by_accession,
                       "Last Modified": self.mod_time,
                       "Atlas Eligibility": self.atlas_eligibility_by_accession,
                       "Curator": self.curators_by_acession,
                       "min_status": self.accession_min_status, # filter
                       "max_status": self.accession_min_status # filter
                       }
        # input_dicts.update(self.extracted_metadata)
        input_data = {}
        for colname, input_dict in input_dicts.items():
            for accession, value in input_dict.items():
                if accession not in input_data:
                    input_data[accession] = {colname: value}
                else:
                    input_data[accession].update({colname: value})

        # df parsing/filtering
        full_df = pd.DataFrame.from_dict(input_data, orient='index')
        nan_filtered_df = full_df[pd.notnull(full_df['Status'])] # filter if status is missing (ID found in DB not in config loc)

        nan_filtered_df['min_order_index'] = nan_filtered_df.apply(lambda x: self.status_type_order.index(x['min_status']), axis=1) # add index column

        pre_analysis_df = nan_filtered_df[(nan_filtered_df["min_order_index"] < 3)]  # filter out loading and lower (index based see status_type_order!)
        post_analysis_df = nan_filtered_df[(nan_filtered_df["min_order_index"] >= 3)]  # filter out loading and lower (index based see status_type_order!)

        # order and collect dfs
        output_dfs = OrderedDict()
        output_dfs["Pre Analysis"] = pre_analysis_df
        output_dfs["Post Analysis"] = post_analysis_df

        # remove columns
        remove_cols = ['min_status', 'max_status', 'min_order_index']
        for name, df in output_dfs.items():
            output_dfs[name] = df.drop(remove_cols, axis=1)

        return output_dfs






# todo fix bug pickle fails when verbose mode is turned off
# todo fix bug. confirm that the two curator dicts are merged before input to the table.