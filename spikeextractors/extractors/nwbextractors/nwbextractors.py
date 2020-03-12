import os
import uuid
from datetime import datetime
from collections import defaultdict
from pathlib import Path

import numpy as np


import spikeextractors as se

try:
    from pynwb import NWBHDF5IO
    from pynwb import NWBFile
    from pynwb.ecephys import ElectricalSeries
    from pynwb.ecephys import ElectrodeGroup
    from pynwb.device import Device
    from pynwb.misc import Units
    
    from hdmf.data_utils import DataChunkIterator

    HAVE_NWB = True
except ModuleNotFoundError:
    HAVE_NWB = False


def check_nwb_install():
    assert HAVE_NWB, "To use the Nwb extractors, install pynwb: \n\n pip install pynwb\n\n"


def set_dynamic_table_property(dynamic_table, row_ids, property_name, values, index=False,
                               default_value=np.nan, description='no description'):
    check_nwb_install()
    if not isinstance(row_ids, list) or not all(isinstance(x, int) for x in row_ids):
        raise TypeError("'ids' must be a list of integers")
    ids = list(dynamic_table.id[:])
    if any([i not in ids for i in row_ids]):
        raise ValueError("'ids' contains values outside the range of existing ids")
    if not isinstance(property_name, str):
        raise TypeError("'property_name' must be a string")
    if len(row_ids) != len(values) and index is False:
        raise ValueError("'ids' and 'values' should be lists of same size")

    if index is False:
        if property_name in dynamic_table:
            for (row_id, value) in zip(row_ids, values):
                dynamic_table[property_name].data[ids.index(row_id)] = value
        else:
            col_data = [default_value] * len(ids)  # init with default val
            for (row_id, value) in zip(row_ids, values):
                col_data[ids.index(row_id)] = value
            dynamic_table.add_column(
                name=property_name,
                description=description,
                data=col_data,
                index=index
            )
    else:
        if property_name in dynamic_table:
            # TODO
            raise NotImplementedError
        else:
            dynamic_table.add_column(
                name=property_name,
                description=description,
                data=values,
                index=index
            )


def get_dynamic_table_property(dynamic_table, *, row_ids=None, property_name):
    all_row_ids = list(dynamic_table.id[:])
    if row_ids is None:
        row_ids = all_row_ids
    return [dynamic_table[property_name][all_row_ids.index(x)] for x in row_ids]


def find_all_unit_property_names(properties_dict: dict, features_dict: dict):
    """
    Finds all existing units properties and units spikes features in the sorting
    dictionaries.
    """
    properties_set = set()
    for k, v in properties_dict.items():
        properties_set.update(list(v.keys()))

    features_set = set()
    for k, v in features_dict.items():
        features_set.update(list(v.keys()))

    return properties_set, features_set


def get_nspikes(units_table, unit_id):
    """Returns the number of spikes for chosen unit."""
    check_nwb_install()
    if unit_id not in units_table.id[:]:
        raise ValueError(str(unit_id) + " is an invalid unit_id. "
                         "Valid ids: " + str(units_table.id[:].tolist()))
    nSpikes = np.diff([0] + list(units_table['spike_times_index'].data[:])).tolist()
    ind = np.where(np.array(units_table.id[:]) == unit_id)[0][0]
    return nSpikes[ind]


def most_relevant_ch(traces):
    """
    Calculates the most relevant channel for an Unit.
    Estimates the channel where the max-min difference of the average traces is greatest.

    traces : ndarray
        ndarray of shape (nSpikes, nChannels, nSamples)
    """
    n_channels = traces.shape[1]
    avg = np.mean(traces, axis=0)

    max_min = np.zeros(n_channels)
    for ch in range(n_channels):
        max_min[ch] = avg[ch, :].max() - avg[ch, :].min()

    relevant_ch = np.argmax(max_min)
    return relevant_ch


class NwbRecordingExtractor(se.RecordingExtractor):
    extractor_name = 'NwbRecording'
    has_default_locations = True
    installed = HAVE_NWB  # check at class level if installed or not
    is_writable = True
    is_dumpable = True
    mode = 'file'
    extractor_gui_params = [
        {'name': 'file_path', 'type': 'file', 'title': "Path to file (.h5 or .hdf5)"},
        {'name': 'acquisition_name', 'type': 'string', 'value': None, 'default': None,
         'title': "Name of Acquisition Method"},
    ]
    installation_mesg = "To use the Nwb extractors, install pynwb: \n\n pip install pynwb\n\n"

    def __init__(self, file_path, electrical_series_name='ElectricalSeries'):
        """

        Parameters
        ----------
        file_path: path to NWB file
        electrical_series_name: str, optional
        """
        check_nwb_install()
        se.RecordingExtractor.__init__(self)
        self._path = file_path
        with NWBHDF5IO(self._path, 'a') as io:
            nwbfile = io.read()
            if electrical_series_name is not None:
                self._electrical_series_name = electrical_series_name
            else:
                a_names = list(nwbfile.acquisition)
                if len(a_names) > 1:
                    raise ValueError('More than one acquisition found. You must specify electrical_series.')
                if len(a_names) == 0:
                    raise ValueError('No acquisitions found in the .nwb file.')
                self._electrical_series_name = a_names[0]
            es = nwbfile.acquisition[self._electrical_series_name]
            if hasattr(es, 'timestamps') and es.timestamps:
                self.sampling_frequency = 1. / np.median(np.diff(es.timestamps))
                self.recording_start_time = es.timestamps[0]
            else:
                self.sampling_frequency = es.rate
                if hasattr(es, 'starting_time'):
                    self.recording_start_time = es.starting_time
                else:
                    self.recording_start_time = 0.

            self.num_frames = int(es.data.shape[0])
            num_channels = len(es.electrodes.table.id[:])

            # Channels gains - for RecordingExtractor, these are values to cast traces to uV
            if es.channel_conversion is not None:
                gains = es.conversion * es.channel_conversion[:] * 1e6
            else:
                gains = es.conversion * np.ones(num_channels) * 1e6

            # Fill channel properties dictionary from electrodes table
            self.channel_ids = es.electrodes.table.id[:]
            self._channel_properties = defaultdict(dict)
            for ind, i in enumerate(self.channel_ids):
                self._channel_properties[i]['gain'] = gains[ind]
                this_loc = []
                if 'rel_x' in nwbfile.electrodes:
                    this_loc.append(nwbfile.electrodes['rel_x'][ind])
                    if 'rel_y' in nwbfile.electrodes:
                        this_loc.append(nwbfile.electrodes['rel_y'][ind])
                    else:
                        this_loc.append(0)
                    self._channel_properties[i]['location'] = this_loc

                for col in nwbfile.electrodes.colnames:
                    if isinstance(nwbfile.electrodes[col][ind], ElectrodeGroup):
                        continue
                    elif col == 'group_name':
                        self._channel_properties[i]['group'] = int(nwbfile.electrodes[col][ind])
                    elif col == 'location':
                        self._channel_properties[i]['brain_area'] = nwbfile.electrodes[col][ind]
                    else:
                        self._channel_properties[i][col] = nwbfile.electrodes[col][ind]

            # Fill epochs dictionary
            self._epochs = {}
            if nwbfile.epochs is not None:
                df_epochs = nwbfile.epochs.to_dataframe()
                self._epochs = {row['tags'][0]: {
                    'start_frame': self.time_to_frame(row['start_time']),
                    'end_frame': self.time_to_frame(row['stop_time'])}
                    for _, row in df_epochs.iterrows()}
        self.kwargs = {'file_path': str(Path(file_path).absolute()), 'electrical_series_name': electrical_series_name}
        self.append_to_dump_dict()

    def get_traces(self, channel_ids=None, start_frame=None, end_frame=None):
        check_nwb_install()
        start_frame, end_frame = self._cast_start_end_frame(start_frame, end_frame)
        if channel_ids is not None:
            if not isinstance(channel_ids, (list, np.ndarray)):
                raise TypeError("'channel_ids' must be a list or array of integers.")
            if not all([id in self.channel_ids for id in channel_ids]):
                raise ValueError("'channel_ids' contain values outside the range of valid ids.")
        else:
            channel_ids = self.channel_ids
        if start_frame is not None:
            if not isinstance(start_frame, (int, np.integer)):
                raise TypeError("'start_frame' must be an integer")
        else:
            start_frame = 0
        if end_frame is not None:
            if not isinstance(end_frame, (int, np.integer)):
                raise TypeError("'end_frame' must be an integer")

        with NWBHDF5IO(self._path, 'r') as io:
            nwbfile = io.read()
            es = nwbfile.acquisition[self._electrical_series_name]
            if np.array(channel_ids).size > 1 and np.any(np.diff(channel_ids) < 0):
                sorted_idx = np.argsort(channel_ids)
                recordings = es.data[start_frame:end_frame, np.sort(channel_ids)].T
                traces = recordings[sorted_idx, :]
            else:
                traces = es.data[start_frame:end_frame, channel_ids].T
            # This DatasetView and lazy operations will only work within context
            # We're keeping the non-lazy version for now
            # es_view = DatasetView(es.data)  # es is an instantiated h5py dataset
            # traces = es_view.lazy_slice[start_frame:end_frame, channel_ids].lazy_transpose()
        return traces

    def get_sampling_frequency(self):
        return self.sampling_frequency

    def get_num_frames(self):
        return self.num_frames

    def get_channel_ids(self):
        return self.channel_ids.tolist()

    @staticmethod
    def write_recording(recording, save_path, acquisition_name='ElectricalSeries', **nwbfile_kwargs):
        '''

        Parameters
        ----------
        recording: RecordingExtractor
        save_path: str
        acquisition_name: str (default 'ElectricalSeries')
        nwbfile_kwargs: optional, pynwb.NWBFile args
        '''
        check_nwb_install()
        n_channels = recording.get_num_channels()
        channel_ids = recording.get_channel_ids()

        if os.path.exists(save_path):
            read_mode = 'r+'
        else:
            read_mode = 'w'

        with NWBHDF5IO(save_path, mode=read_mode) as io:
            if read_mode == 'r+':
                nwbfile = io.read()
            else:
                kwargs = {'session_description': 'No description',
                          'identifier': str(uuid.uuid4()),
                          'session_start_time': datetime.now()}
                kwargs.update(**nwbfile_kwargs)
                nwbfile = NWBFile(**kwargs)

            # Tests if Device already exists
            aux = [isinstance(i, Device) for i in nwbfile.children]
            if any(aux):
                device = nwbfile.children[np.where(aux)[0][0]]
            else:
                device = nwbfile.create_device(name='Device')

            # Check if 'groups' property exists for channels in Recording
            if 'group' in recording.get_shared_channel_property_names():
                el_groups_names = np.unique(recording.get_channel_groups()).tolist()
            else:
                el_groups_names = ["0"]
                # Electrode groups are required for NWB, for consistency we create group for Recording channels
                ids = recording.get_channel_ids()
                vals = [0] * len(ids)
                recording.set_channel_groups(channel_ids=ids, groups=vals)
            # Creates electrode groups for groups names in Recording
            for grp_name in el_groups_names:
                if str(grp_name) not in nwbfile.electrode_groups:
                    elec_group = nwbfile.create_electrode_group(
                        name=str(grp_name),
                        location="electrode_group_location",
                        device=device,
                        description="electrode_group_description"
                    )

            # Check for existing electrodes
            if nwbfile.electrodes is not None:
                nwb_elec_ids = nwbfile.electrodes.id.data[:]
            else:
                nwb_elec_ids = []

            # add new electrodes with id, (x, y, z) and groups
            if nwbfile.electrodes is None or 'rel_x' not in nwbfile.electrodes.colnames:
                nwbfile.add_electrode_column('rel_x', 'x position of electrode in electrode group')
            if nwbfile.electrodes is None or 'rel_y' not in nwbfile.electrodes.colnames:
                nwbfile.add_electrode_column('rel_y', 'y position of electrode in electrode group')
            for m in channel_ids:
                if m not in nwb_elec_ids:
                    location = recording.get_channel_property(m, 'location')
                    while len(location) < 2:
                        location = np.append(location, [0])
                    impedence = -1.0
                    grp_name = recording.get_channel_groups(channel_ids=[m])
                    grp = nwbfile.electrode_groups[str(grp_name[0])]
                    nwbfile.add_electrode(
                        id=m,
                        x=np.nan, y=np.nan, z=np.nan,
                        rel_x=float(location[0]), rel_y=float(location[1]),
                        imp=impedence,
                        location='unknown',
                        filtering='none',
                        group=grp,
                    )
            electrode_table = nwbfile.electrodes

            # add/update electrode properties
            for ch in channel_ids:
                rx_channel_properties = recording.get_channel_property_names(channel_id=ch)
                for pr in rx_channel_properties:
                    val = recording.get_channel_property(ch, pr)
                    desc = 'no description'
                    # property 'location' of RX channels corresponds to x, y, z of NWB electrodes
                    if pr == 'location':
                        names = ['rel_x', 'rel_y']
                        for (nm, v) in zip(names, val):
                            set_dynamic_table_property(
                                dynamic_table=electrode_table,
                                row_ids=[ch],
                                property_name=nm,
                                values=[float(v)],
                                default_value=np.nan,
                                description=nm+' coordinate location on the implant'
                            )
                        continue
                    # property 'group' of electrodes can not be updated
                    if pr == 'group':
                        continue
                    # property 'gain' should not be in the NWB electrodes_table
                    if pr == 'gain':
                        continue
                    # property 'brain_area' of RX channels corresponds to 'location' of NWB electrodes
                    if pr == 'brain_area':
                        pr = 'location'
                        desc = 'brain area location'
                    set_dynamic_table_property(
                        dynamic_table=electrode_table,
                        row_ids=[ch],
                        property_name=pr,
                        values=[val],
                        default_value=np.nan,
                        description=desc
                    )

            # Tests if ElectricalSeries already exists in acquisition
            aux = [isinstance(i, ElectricalSeries) for i in nwbfile.acquisition.values()]
            if not any(aux):
                electrode_table_region = nwbfile.create_electrode_table_region(
                    list(range(n_channels)),
                    'electrode_table_region'
                )

                # sampling rate
                rate = recording.get_sampling_frequency()

                # channels gains - for RecordingExtractor, these are values to cast traces to uV
                # for nwb, the conversions (gains) cast the data to Volts
                gains = np.squeeze([recording.get_channel_gains(channel_ids=[ch])
                         if 'gain' in recording.get_channel_property_names(channel_id=ch) else 1
                         for ch in channel_ids])
                if len(np.unique(gains)) == 1:  # if all gains are equal
                    scalar_conversion = np.unique(gains)[0]*1e-6
                    channel_conversion = None
                else:
                    scalar_conversion = 1.
                    channel_conversion = gains*1e-6

                def data_generator(recording, num_channels):
                    #  generates data chunks for iterator
                    for id in range(0, num_channels):
                        data = recording.get_traces(channel_ids=[id]).flatten()
                        yield data

                data = data_generator(recording=recording, num_channels=n_channels)
                ephys_data = DataChunkIterator(data=data, iter_axis=1)
                acquisition_name = 'ElectricalSeries'

                # If traces are stored as 'int16', then to get Volts = data*channel_conversion*conversion
                ephys_ts = ElectricalSeries(
                    name=acquisition_name,
                    data=ephys_data,
                    electrodes=electrode_table_region,
                    starting_time=recording.frame_to_time(0),
                    rate=rate,
                    conversion=scalar_conversion,
                    channel_conversion=channel_conversion,
                    comments='Generated from SpikeInterface::NwbRecordingExtractor',
                    description='acquisition_description'
                )
                nwbfile.add_acquisition(ephys_ts)

            # add/update epochs
            for (name, ep) in recording._epochs.items():
                if nwbfile.epochs is None:
                    nwbfile.add_epoch(
                        start_time=recording.frame_to_time(ep['start_frame']),
                        stop_time=recording.frame_to_time(ep['end_frame']),
                        tags=name
                    )
                else:
                    if [name] in nwbfile.epochs['tags'][:]:
                        ind = nwbfile.epochs['tags'][:].index([name])
                        nwbfile.epochs['start_time'].data[ind] = recording.frame_to_time(ep['start_frame'])
                        nwbfile.epochs['stop_time'].data[ind] = recording.frame_to_time(ep['end_frame'])
                    else:
                        nwbfile.add_epoch(
                            start_time=recording.frame_to_time(ep['start_frame']),
                            stop_time=recording.frame_to_time(ep['end_frame']),
                            tags=name
                        )

            io.write(nwbfile)


class NwbSortingExtractor(se.SortingExtractor):
    extractor_name = 'NwbSortingExtractor'
    exporter_name = 'NwbSortingExporter'
    exporter_gui_params = [
        {'name': 'save_path', 'type': 'file', 'title': "Save path"},
        {'name': 'identifier', 'type': 'str', 'value': None, 'default': None, 'title': "The session identifier"},
        {'name': 'session_description', 'type': 'str', 'value': None, 'default': None,
         'title': "The session description"},
    ]
    installed = HAVE_NWB  # check at class level if installed or not
    is_writable = True
    mode = 'file'
    installation_mesg = "To use the Nwb extractors, install pynwb: \n\n pip install pynwb\n\n"

    def __init__(self, path, electrical_series=None, sampling_frequency=None):
        """

        Parameters
        ----------
        path: path to NWB file
        electrical_series: pynwb.ecephys.ElectricalSeries object
        """
        check_nwb_install()
        se.SortingExtractor.__init__(self)
        self._path = path
        with NWBHDF5IO(self._path, 'r') as io:
            nwbfile = io.read()
            if sampling_frequency is None:
                # defines the electrical series from where the sorting came from
                # important to know the sampling_frequency
                if electrical_series is None:
                    if len(nwbfile.acquisition) > 1:
                        raise Exception('More than one acquisition found. You must specify electrical_series.')
                    if len(nwbfile.acquisition) == 0:
                        raise Exception('No acquisitions found in the .nwb file.')
                    es = list(nwbfile.acquisition.values())[0]
                else:
                    es = electrical_series
                # get rate
                if es.rate is not None:
                    self._sampling_frequency = es.rate
                else:
                    self._sampling_frequency = 1 / (es.timestamps[1] - es.timestamps[0])
            else:
                self._sampling_frequency = sampling_frequency

            # get all units ids
            units_ids = nwbfile.units.id[:]

            # store units properties and spike features to dictionaries
            all_pr_ft = list(nwbfile.units.colnames)
            all_names = [i.name for i in nwbfile.units.columns]
            for item in all_pr_ft:
                if item == 'spike_times':
                    continue
                # test if item is a unit_property or a spike_feature
                if item+'_index' in all_names:  # if it has index, it is a spike_feature
                    for id in units_ids:
                        ind = list(units_ids).index(id)
                        self._unit_features.update({id: {item: nwbfile.units[item][ind]}})
                else:  # if it is unit_property
                    for id in units_ids:
                        ind = list(units_ids).index(id)
                        self._unit_properties.update({id: {item: nwbfile.units[item][ind]}})

            # Fill epochs dictionary
            self._epochs = {}
            if nwbfile.epochs is not None:
                df_epochs = nwbfile.epochs.to_dataframe()
                self._epochs = {row['tags'][0]: {
                    'start_frame': self.time_to_frame(row['start_time']),
                    'end_frame': self.time_to_frame(row['stop_time'])}
                    for _, row in df_epochs.iterrows()}


    def get_unit_ids(self):
        '''This function returns a list of ids (ints) for each unit in the sorted result.

        Returns
        ----------
        unit_ids: array_like
            A list of the unit ids in the sorted result (ints).
        '''
        check_nwb_install()
        with NWBHDF5IO(self._path, 'r') as io:
            nwbfile = io.read()
            unit_ids = [int(i) for i in nwbfile.units.id[:]]
        return unit_ids

    def get_unit_spike_train(self, unit_id, start_frame=None, end_frame=None):
        start_frame, end_frame = self._cast_start_end_frame(start_frame, end_frame)
        if start_frame is None:
            start_frame = 0
        if end_frame is None:
            end_frame = np.Inf
        check_nwb_install()
        with NWBHDF5IO(self._path, 'r') as io:
            nwbfile = io.read()
            # chosen unit and interval
            times = nwbfile.units['spike_times'][list(nwbfile.units.id[:]).index(unit_id)][:]
            # spike times are measured in samples
            frames = self.time_to_frame(times)
        return frames[(frames > start_frame) & (frames < end_frame)]

    def time_to_frame(self, time):
        return np.round(time * self.get_sampling_frequency()).astype('int')

    def frame_to_time(self, frame):
        return frame / self.get_sampling_frequency()

    @staticmethod
    def write_sorting(sorting, save_path, **nwbfile_kwargs):
        """

        Parameters
        ----------
        sorting: SortingExtractor
        save_path: str
        nwbfile_kwargs: optional, pynwb.NWBFile args
        """
        check_nwb_install()

        ids = sorting.get_unit_ids()
        fs = sorting.get_sampling_frequency()
        if hasattr(sorting, '_t0'):
            t0 = sorting._t0
        else:
            t0 = 0.

        (all_properties, all_features) = find_all_unit_property_names(
            properties_dict=sorting._unit_properties,
            features_dict=sorting._unit_features
        )

        if os.path.exists(save_path):
            read_mode = 'r+'
        else:
            read_mode = 'w'

        with NWBHDF5IO(save_path, mode=read_mode) as io:
            if read_mode == 'r+':
                nwbfile = io.read()
            else:
                kwargs = {'session_description': 'No description',
                          'identifier': str(uuid.uuid4()),
                          'session_start_time': datetime.now()}
                kwargs.update(**nwbfile_kwargs)
                nwbfile = NWBFile(**kwargs)

            # If no Units present in mwb file
            if nwbfile.units is None:
                for id in ids:
                    spkt = sorting.get_unit_spike_train(unit_id=id) / fs
                    nwbfile.add_unit(id=id, spike_times=spkt)

            # Units properties
            for pr in all_properties:
                unit_ids = [int(k) for k, v in sorting._unit_properties.items()
                            if pr in v]
                vals = [v[pr] for k, v in sorting._unit_properties.items()
                        if pr in v]
                set_dynamic_table_property(
                    dynamic_table=nwbfile.units,
                    row_ids=unit_ids,
                    property_name=pr,
                    values=vals,
                    default_value=np.nan,
                    description='no description'
                )

            # # Stores average and std of spike traces
            # if 'waveforms' in sorting.get_unit_spike_feature_names(unit_id=id):
            #     wf = sorting.get_unit_spike_features(unit_id=id,
            #                                          feature_name='waveforms')
            #     relevant_ch = most_relevant_ch(wf)
            #     # Spike traces on the most relevant channel
            #     traces = wf[:, relevant_ch, :]
            #     traces_avg = np.mean(traces, axis=0)
            #     traces_std = np.std(traces, axis=0)
            #     nwbfile.add_unit(
            #         id=id,
            #         spike_times=spkt,
            #         waveform_mean=traces_avg,
            #         waveform_sd=traces_std
            #     )

            # Units spike features
            nspikes = {k: get_nspikes(nwbfile.units, int(k)) for k in ids}
            for ft in all_features:
                vals = [v[ft] if ft in v else [np.nan] * nspikes[int(k)]
                        for k, v in sorting._unit_features.items()]
                flatten_vals = [item for sublist in vals for item in sublist]
                nspks_list = [sp for sp in nspikes.values()]
                spikes_index = np.cumsum(nspks_list).tolist()
                set_dynamic_table_property(
                    dynamic_table=nwbfile.units,
                    row_ids=ids,
                    property_name=ft,
                    values=flatten_vals,
                    index=spikes_index,
                )

            io.write(nwbfile)
