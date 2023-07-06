from datetime import datetime, timedelta

import time
import singer
import json
from singer import metrics, metadata, Transformer
from singer.bookmarks import set_currently_syncing

from tap_playvox.discover import discover
from tap_playvox.endpoints import ENDPOINTS_CONFIG

LOGGER = singer.get_logger()

def write_schema(stream):
    schema = stream.schema.to_dict()
    singer.write_schema(stream.tap_stream_id, schema, stream.key_properties)
            
def sync_endpoint(client,
                  catalog,
                  state,
                  required_streams,
                  selected_streams,
                  stream_name,
                  endpoint,
                  key_bag,
                  stream_params={}):
    
    persist = endpoint.get('persist', True)

    if persist:
        stream = catalog.get_stream(stream_name)
        schema = stream.schema.to_dict()
        mdata = metadata.to_map(stream.metadata)
        write_schema(stream)
    
    # API Parameters
    # Reference https://support.agyletime.com/hc/en-us/articles/4402740869401-Integration-Guide-Agent-Metrics
    initial_load = True
    next_page_token=''
  
    params={}
    
    #ISO 8601 Datetime format
    iso_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    start_date = singer.get_bookmark(state,
                                     stream_name,
                                     client.start_date)

    if start_date:
        start_datetime = singer.utils.strptime_to_utc(start_date)
    else:
        # If no start_date or bookmark available, default to start_date of the config
        start_datetime = singer.utils.strptime_to_utc(client.start_date)
        
        dt_string = str(start_datetime)
        dt_object = datetime.fromisoformat(dt_string)
        start_datetime = dt_object.strftime(iso_format)
    
    if stream_name == 'tasks':
        params = {
            'taskStartTimeFrom': str(start_datetime),
            'taskStartTimeTo': str(datetime.utcnow().isoformat())
        }
 
    if stream_name == 'metrics':
        params = {
            'startTime': str(start_datetime),
            'userGrouping': 'true',
            'dateGrouping': 'true'
        }
    path = endpoint['path'].format(**key_bag)
    
    #next_page_token param value for Playvox
    while initial_load or len(next_page_token) > 0:

        if initial_load:
            initial_load = False
        if state.get('currently_syncing') != stream_name:
            # We may have just been syncing a child stream.
            # Update the currently syncing stream if needed.
            update_current_stream(state, stream_name)

        data = client.get(path,
                              params=params,
                              endpoint=stream_name,
                             )
        
        if data is None:
            return
        
        if 'data_key' in endpoint:
            records = data[endpoint['data_key']]
        else:
            records = [data]      
        
        #if no records are received
        if not records:
            return
        
        #parse records
        parse=True
        
        date_records = []
        parsed_data_records = 0
        # For Worstream Agent Metrics, only parse the data if Users data is presents
        if stream_name == 'metrics' and endpoint['metric_key'] in data[endpoint['data_key']]:
            date_records = data[endpoint['data_key']][endpoint['metric_key']]
                                                                      
        with metrics.record_counter(stream_name) as counter:
                with Transformer() as transformer:
                    while parse is True:
                        #parse through all date records for Workstream Agent Metrics 
                        if(len(date_records)>=1):
                            record_date = data[endpoint['data_key']][endpoint['metric_key']][parsed_data_records]['date'] 
                            records = data[endpoint['data_key']][endpoint['metric_key']][parsed_data_records][endpoint['user_key']]
                            parsed_data_records += 1
                            
                        for record in records:
                            if persist and stream_name in selected_streams:
                                record = {**record, **key_bag}
                                try:
                                    record_typed = transformer.transform(record,
                                                                    schema,
                                                                    mdata)  
                                    
                                    #Map 'date' for Workstream Agent Metrics
                                    if (len(date_records)>=1):
                                        record_typed["date"] = record_date
                                        
                                except Exception as e:
                                    LOGGER.info("PLAYVOX Sync Exception: %s....Record: %s", e, record)
                                
                                singer.write_record(stream_name, record_typed)
                                
                                counter.increment()
                        
                        if len(date_records)==parsed_data_records:
                            parse = False
                            
        # 1. set bookmark
        singer.write_bookmark(state, stream_name, 'endDate', str(datetime.utcnow().isoformat()))
                
        # for records that expect to be paged
        if endpoint.get('paginate', True):
            #get next_page_token
            next_page_token = data.get('nextPageToken', '')
            #set 'nextPageToken' token when results have pages
            params = {
                        'nextPageToken': next_page_token
                    }
                    
def update_current_stream(state, stream_name=None):  
    set_currently_syncing(state, stream_name) 
    singer.write_state(state)

def get_required_streams(endpoints, selected_stream_names):
    required_streams = []
    for name, endpoint in endpoints.items():
        child_required_streams = None
        if 'children' in endpoint:
            child_required_streams = get_required_streams(endpoint['children'],
                                                          selected_stream_names)
        if name in selected_stream_names or child_required_streams:
            required_streams.append(name)
            if child_required_streams:
                required_streams += child_required_streams

    return required_streams

def sync(client, catalog, state):
    if not catalog:
        catalog = discover()
        selected_streams = catalog.streams
    else:
        selected_streams = catalog.get_selected_streams(state)

    selected_stream_names = []
    for selected_stream in selected_streams:
        selected_stream_names.append(selected_stream.tap_stream_id)

    required_streams = get_required_streams(ENDPOINTS_CONFIG, selected_stream_names)

    for stream_name, endpoint in ENDPOINTS_CONFIG.items():
        if stream_name in required_streams:
            update_current_stream(state, stream_name)
            sync_endpoint(client,        
                          catalog,
                          state,
                          required_streams,
                          selected_stream_names,
                          stream_name,
                          endpoint,
                          {})

    update_current_stream(state)
    
    return state
