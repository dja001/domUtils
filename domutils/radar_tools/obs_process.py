
#identify logger names based on package hierarchy
import numpy as np
import dask
import dask.array 
import dask.distributed

def _setup_logging(args, is_worker=False, sub_dir=''):
    """setup logger and handlers

    Because of parallel execution, logging has to be setup for every forked processes
    so it is put in this reusable function
    """

    import sys 
    import logging
    import domutils._py_tools as dpy

    # logging is configured to write everything to stdout in addition to a log file
    # in a 'logs' directory
    logging_basename = 'domutils.radar_tools'
    logger = logging.getLogger(logging_basename)
    # if this is a newly created logger, it will have no handlers
    if not len(logger.handlers):
        #make sure 'logs' directory exists and is empty
        dpy.parallel_mkdir('logs')

        logging.captureWarnings(True)
        logger.setLevel(args.log_level)
        #handlers
        stream_handler = logging.StreamHandler(sys.stdout)
        if is_worker:
            dpy.parallel_mkdir('logs/'+sub_dir)
            worker_id = str(dask.distributed.get_worker().id)
            file_handler = logging.FileHandler('logs/'+sub_dir+'/'+worker_id, 'w')
        else:
            file_handler = logging.FileHandler('logs/obs_process.log', 'w')
        #levels
        stream_handler.setLevel(args.log_level)
        file_handler.setLevel(args.log_level)
        #format
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        if is_worker:
            stream_formatter = logging.Formatter(worker_id+'    %(message)s')
        else:
            stream_formatter = file_formatter
        stream_handler.setFormatter(stream_formatter)
        file_handler.setFormatter(file_formatter)
        #add handlers
        logger.addHandler(stream_handler)
        logger.addHandler(file_handler)

    return logger

@dask.delayed
def _dask_process_at_one_time(*args, **kwargs):

    #setup logger for dask worker if not already done
    command_line_args = args[2]
    logger = _setup_logging(command_line_args, is_worker=True, sub_dir='process_at_one_time')

    return _process_at_one_time(*args, **kwargs)


def _process_at_one_time(valid_date, fst_template, args):
    #output data to std file

    import os
    import time
    import copy
    import rpnpy.librmn.all as rmn
    from rpnpy.rpndate import RPNDate
    from domutils import radar_tools
    from domcmc import fst_tools
    import domutils._py_tools as dpy

    logger = _setup_logging(args)

    logger.info(f'_process_at_one_time starting to process date: {valid_date}')

    #output filename and directory
    output_file = args.output_dir + valid_date.strftime(args.output_file_struc)
    # check if we need to process this date
    if os.path.isfile(output_file):
        # output file is there, check if data is there at this time. 
        pr = fst_tools.get_data(var_name='RDPR', file_name=output_file, datev=valid_date)
        qi = fst_tools.get_data(var_name='RDQI', file_name=output_file, datev=valid_date)
        if (pr is not None) and (qi is not None):
            if args.complete_dataset:
                logger.info(f'{output_file} exists, desired data is there and complete_dataset=True. Skipping to the next.')
                return np.array([1], dtype=float)
            else:
                raise RuntimeError(f'{output_file} exists, desired data is there and complete_dataset=False. Remove output file before retrying')
    else:
        #we will create or overwrite the file; before that make sure directory exists
        this_fst_dir = os.path.dirname(output_file)
        dpy.parallel_mkdir(this_fst_dir)
    
    #get destination grid from PR template
    dest_lon = fst_template['lon']
    dest_lat = fst_template['lat']

    #reading the data
    if args.accum_len is not None:
        desired_quantity='accumulation'
        fst_var_name='PR'
        dat_dict = radar_tools.get_accumulation(end_date=valid_date,
                                                duration=args.accum_len,
                                                desired_quantity=desired_quantity,
                                                data_path=args.input_data_dir,
                                                odim_latlon_file=args.h5_latlon_file,
                                                data_recipe=args.input_file_struc,
                                                dest_lon=dest_lon,
                                                dest_lat=dest_lat,
                                                median_filt=args.median_filt,
                                                smooth_radius=args.smooth_radius)

        if desired_quantity == 'accumulation':
            logger.warning('!!!convert mm to m since PR quantity is outputted!!!')
            dat_dict['accumulation'] /= 1e3
        data_quantity_name  = desired_quantity
        data_date_name      = 'end_date'
    else:
        #
        #if reflectivity is desired use:
        #desired_quantity='reflectivity'
        #fst_var_name='RDBZ'

        #
        #for precipitation rates used in LHN use:
        desired_quantity='precip_rate'
        fst_var_name='RDPR'

        #get, convert, interpolate and smooth ODIM Reflectivity mosaics
        dat_dict = radar_tools.get_instantaneous(valid_date=valid_date,
                                                 desired_quantity=desired_quantity,
                                                 data_path=args.input_data_dir,
                                                 odim_latlon_file=args.h5_latlon_file,
                                                 data_recipe=args.input_file_struc,
                                                 dest_lon=dest_lon,
                                                 dest_lat=dest_lat,
                                                 median_filt=args.median_filt,
                                                 smooth_radius=args.smooth_radius)
        data_quantity_name  = desired_quantity
        data_date_name      = 'valid_date'

    #if we got nothing, fill output with nodata and zeros
    if dat_dict is None:
        logger.warning('no data found or file unreadeable, observations are set to -9999. with quality index = 0.')
        expected_shape  = dest_lat.shape
        precip_rate     = np.full(expected_shape, -9999.)
        quality_index   = np.zeros(expected_shape)
        data_valid_date = valid_date
    else:
        precip_rate     = dat_dict[data_quantity_name]
        quality_index   = dat_dict['total_quality_index']
        data_valid_date = dat_dict[data_date_name]

    #mettre etiket
    if args.median_filt is None :
        etiquette_median_filt = 0
    else:
        etiquette_median_filt = args.median_filt
    if args.smooth_radius is None :
        etiquette_smooth_radius = 0
    else:
        etiquette_smooth_radius = args.smooth_radius
    etiket = 'MED'+"{:1d}".format(etiquette_median_filt)+'SM'+"{:02d}".format(etiquette_smooth_radius)

    _write_fst_file(valid_date, precip_rate, quality_index, args, etiket=etiket)

    #make a figure for this std file if the argument figure_dir was provided
    if args.figure_dir is not None:
        radar_tools.plot_rdpr_rdqi(fst_file=output_file, 
                                   this_date=valid_date,
                                   args=args)

    return np.array([1], dtype=float)



def _parse_num(arg, dtype='int', deltat_seconds=False):
    """
    change string to number

    parses m1p4 to -1.4
    removes preceding zeros

    with deltat_seconds=True output will always be in seconds
    S = seconds; M = minutes; if nothing minutes are assumed
    2S -> 2     (seconds)
    2M -> 120   (seconds)
    2  -> 120   (seconds)

    return desired type
    """

    if isinstance(arg, str):
        if deltat_seconds:
            if   arg[-1] == 'S':
                num_str = arg[:-1]
            elif arg[-1] == 'M':
                num_str = float(arg[:-1]) * 60.
            else:
                # mabe later I will require a time specification
                num_str = float(arg[:-1]) * 60.
        else:
            num_str = arg.lstrip('0').replace('p','.').replace('m','-')
            if num_str == '':
                num_str = 0
    else:
        num_str = arg

    if dtype == 'int':
        out = int(num_str)
    elif dtype == 'float':
        out = float(num_str)
    else:
        raise ValueError('unknown dtype')

    return out


def _to_datetime(time_str):
    """
    changes string yyyymmddhhmmss to python datetime object
    """
    import datetime

    yyyy = _parse_num(time_str[0:4])
    mo   = _parse_num(time_str[4:6])
    dd   = _parse_num(time_str[6:8])
    hh   = _parse_num(time_str[8:10])
    mi   = _parse_num(time_str[10:12])
    ss   = 0
    return datetime.datetime(yyyy,mo,dd,hh,mi,ss)


def _process_a_bunch_of_times(args):
    """ read odim H5, manipulate it, and write to fst

    depending on the number of cpus, serial execution or parallel execution with multiprocessing will be chosen 
    """

    import os
    import datetime
    import glob
    import signal
    import time
    from domcmc import fst_tools


    #logging
    logger = _setup_logging(args)

    logger.info(f'getting output domain from: {args.sample_pr_file}')
    fst_template = fst_tools.get_data(args.sample_pr_file, var_name='PR', latlon=True)
    if fst_template is None:
        raise ValueError('Problem getting PR from: ',args.sample_pr_file )

    #if only 1 cpu, do serial execution in a for loop
    # makes for easier debugging 
    if args.ncores == 1 :
        #serial execution
        logger.info('Launching SERIAL execution of obs processing')
        for this_date in args.input_date_list:
            _process_at_one_time(this_date, fst_template, args)
    else :
        #parallel conversion with nultiprocessing
        logger.info('Launching PARALLEL execution of observation processing')

        tt1 = time.time()
        #delay input data 
        delayed_args         = dask.delayed(args)
        delayed_fst_template = dask.delayed(fst_template)

        #delayed list of results
        res_list = [dask.array.from_delayed(_dask_process_at_one_time(this_date, 
                                                                      delayed_fst_template, 
                                                                      delayed_args), (1,), float) for this_date in args.input_date_list ]

        #what output do we want
        res_stack = dask.array.stack(res_list)
                
        # computation happens here
        big_arr = res_stack.compute()

        tt2 = time.time()
        logger.info(f'Parallel execution done; walltime: {tt2-tt1} seconds')

        # check that we received all correct termination
        if big_arr.sum() != len(args.input_date_list) :
            raise RuntimeError('did not receive correct termination from all processes')

@dask.delayed
def _dask_motion_vector_at_one_time(*args, **kwargs):

    #setup logger for dask worker if not already done
    command_line_args = args[1]
    logger = _setup_logging(command_line_args, is_worker=True, sub_dir='motion_vector_at_one_time')

    return _motion_vector_at_one_time(*args, **kwargs)

def _motion_vector_at_one_time(this_time, args):
    """ read 3 precip field and compute motion vectors at one time

    re-reading precip fields is not the most efficient but the time needed to do this
    is negligible compared to computing motion vectors
    """

    import os
    import domutils
    import domutils._py_tools as dpy
    import pysteps
    from domcmc import fst_tools

    logger = _setup_logging(args)

    logger.info(f'_motion_vector_at_one_time starting; end_time:{this_time}')

    # the file we want to write to
    output_file = args.motion_vectors_dir + this_time.strftime('%Y%m%d%H%M_end_window.npz')

    if (os.path.isfile(output_file) and args.complete_dataset):
        logger.info(f'{output_file} exists and complete_dataset=True. Skipping to the next.')
        return np.array([1], dtype=float)
    elif os.path.isfile(output_file):
        #file exists but we are not completing a dataset erase file before making a new one
        os.remove(output_file)
    else:
        #we will create a new file; before that make sure directory exists
        dpy.parallel_mkdir(args.motion_vectors_dir)

    # index in time array
    tt = args.input_date_list.index(this_time)

    # number of timesteps for motion vectors
    nt = 3
    z_acc = np.zeros((nt, args.out_ny, args.out_nx))

    # read precip, convert to reflectivity and reshape to make pysteps happy
    # t0 - 2dt
    fst_file = args.output_dir+args.input_date_list[tt-2].strftime(args.output_file_struc)
    z_v = fst_tools.get_data(var_name='RDPR', file_name=fst_file, datev=args.input_date_list[tt-2])['values']
    if args.median_filt_before_mv is not None:
        median_inds = domutils.radar_tools.median_filter.get_inds(z_v, window=args.median_filt_before_mv)
        z_v = domutils.radar_tools.median_filter.apply_inds(z_v, median_inds)
    z_acc[0,:,:] = np.transpose(domutils.radar_tools.exponential_zr(z_v, r_to_dbz=True))
    # t0 - 1dt
    fst_file = args.output_dir+args.input_date_list[tt-1].strftime(args.output_file_struc)
    z_v = fst_tools.get_data(var_name='RDPR', file_name=fst_file, datev=args.input_date_list[tt-1])['values']
    if args.median_filt_before_mv is not None:
        median_inds = domutils.radar_tools.median_filter.get_inds(z_v, window=args.median_filt_before_mv)
        z_v = domutils.radar_tools.median_filter.apply_inds(z_v, median_inds)
    z_acc[1,:,:] = np.transpose(domutils.radar_tools.exponential_zr(z_v, r_to_dbz=True))
    # t0 
    fst_file = args.output_dir+args.input_date_list[tt].strftime(args.output_file_struc)
    z_v = fst_tools.get_data(var_name='RDPR', file_name=fst_file, datev=args.input_date_list[tt  ])['values']
    if args.median_filt_before_mv is not None:
        median_inds = domutils.radar_tools.median_filter.get_inds(z_v, window=args.median_filt_before_mv)
        z_v = domutils.radar_tools.median_filter.apply_inds(z_v, median_inds)
    z_acc[2,:,:] = np.transpose(domutils.radar_tools.exponential_zr(z_v, r_to_dbz=True))

    # remove small values
    min_dbz = 0.
    z_acc = np.where(z_acc < min_dbz, 0., z_acc)

    # compute motion vectors
    oflow_method = pysteps.motion.get_method("LK")
    uv_motion = oflow_method(z_acc)

    #save output to file
    try:
        np.savez_compressed(output_file, uv_motion=uv_motion)
    except:
        raise RuntimeError(f'problem writing: {output_file}')
    
   
    logger.info(f'_motion_vector_at_one_time done; end_time:{this_time}')
    
    return np.array([1], dtype=float)


def _make_motion_vectors(args):
    """ compute motion vectors for radar observations available at a evenly separated times

    depending on the number of cpus, serial execution or parallel execution with multiprocessing will be chosen 
    """

    import time
    import glob
    import signal
    import dask
    from domcmc import fst_tools

    #logging
    logger = _setup_logging(args)

    #read first entry to get dimensions
    fst_file = args.processed_dir+args.input_date_list[0].strftime(args.output_file_struc)
    z_v = fst_tools.get_data(var_name='RDPR', file_name=fst_file, datev=args.input_date_list[0])['values']
    nx, ny = z_v.shape
    args.out_nx = nx
    args.out_ny = ny

    #for output we keep pysteps dim convention ny, ny, nx
    nt = len(args.input_date_list)-2


    if args.ncores == 1 :
        #serial execution
        logger.info('Launching SERIAL computation of motion vectors')
        big_arr = np.zeros((nt,))
        for tt, this_time in enumerate(args.input_date_list[2:]):

            this_result = _motion_vector_at_one_time(this_time, args)
            
            #shift before next round
            big_arr[tt] = this_result

    else:
        #parallel execution
        logger.info('Launching PARALLEL computation of motion vectors')

        tt1 = time.time()

        #delay input data 
        delayed_args = dask.delayed(args)

        #delayed list of results
        res_list = [dask.array.from_delayed(_dask_motion_vector_at_one_time(this_date, delayed_args), (1,), float) for this_date in args.input_date_list[2:] ]

        #what output do we want
        res_stack = dask.array.stack(res_list)
                
        # computation happens here
        big_arr = res_stack.compute()

        tt2 = time.time()
        logger.info(f'Parallel execution done; walltime: {tt2-tt1} seconds')

    if int(np.sum(big_arr)) != len(args.input_date_list[2:]):
        raise RuntimeError('Number of sucess run is not the the same as the number of output times')

def _scale_wind(uv, fact_before):
    """scale wind vector for mid timesteps
    """

    # conversion to rho theta (met angle convention)
    rho = np.sqrt(uv[0,:,:]**2. + uv[1,:,:]**2.)
    theta = np.arctan2(uv[0,:,:],uv[1,:,:])

    # scale modulus
    rho *= fact_before
    uv[0,:,:] = rho * np.sin(theta)
    uv[1,:,:] = rho * np.cos(theta)

    # return scaled u and v components
    return uv


@dask.delayed
def _dask_t_interp_at_one_time(*args, **kwargs):

    #setup logger for dask worker if not already done
    command_line_args = args[1]
    logger = _setup_logging(command_line_args, is_worker=True, sub_dir='t_interp_at_one_time')

    return _t_interp_at_one_time(*args, **kwargs)

def _t_interp_at_one_time(out_time, args):
    """nowcasting time interpolation using forward and backward advection
    """

    import os
    import datetime
    from domcmc import fst_tools
    import domutils._py_tools as dpy
    from domutils import radar_tools
    import pysteps 
    import time

    missing = -9999.

    # logging
    logger = _setup_logging(args)

    fst_output_file = args.output_dir + out_time.strftime(args.tinterpolated_file_struc)
    logger.info(f'Working on: {os.path.basename(fst_output_file)}')
    if os.path.isfile(fst_output_file):
        # output file is there, check if data is there at this time. 
        # it is necessary to obtain a lock on file since other processes may be trying to write to it 
        # and cause crashes
        _aquire_lock(fst_output_file)
        pr = fst_tools.get_data(var_name='RDPR', file_name=fst_output_file, datev=out_time)
        qi = fst_tools.get_data(var_name='RDQI', file_name=fst_output_file, datev=out_time)
        _release_lock(fst_output_file)
        if (pr is not None) and (qi is not None):
            if args.complete_dataset:
                logger.info(f'{fst_output_file} exists, desired data is there and complete_dataset=True. Skipping to the next.')
                return np.array([1], dtype=float)
            else:
                raise RuntimeError(f'{fst_output_file} exists, desired data is there and complete_dataset=False. Remove output file before retrying')
    else:
        #we will create a new file; before that make sure directory exists
        dpy.parallel_mkdir(args.output_dir)

    # all inputs within range to participate in average
    t_max = out_time + datetime.timedelta(seconds=args.interp_max_dt)
    t_min = out_time - datetime.timedelta(seconds=args.interp_max_dt)
    participating_list = []
    for input_time in args.input_date_list:
        if (input_time >= t_min) and (input_time <= t_max):
            dt = (out_time - input_time).total_seconds()
            # TODO determine a better weighting function for this
            weight = 1./(np.abs(dt)+100.)
            participating_list.append({'vtime'  : input_time,
                                       'dt'     : dt,
                                       'weight' : weight
                                      })
    # if we are extrapolating, the last output data should always be part of the list
    last_input_time = args.input_date_list[-1]
    if out_time > last_input_time:
        last_input_is_in_list = False
        for item in participating_list:
            if item['vtime'] == last_input_time:
                last_input_is_in_list = True
                break
        if not last_input_is_in_list:
            dt = (out_time - last_input_time).total_seconds()
            weight = 1./(np.abs(dt)+100.)
            participating_list.append({'vtime'  : last_input_time,
                                       'dt'     : dt,
                                       'weight' : weight
                                      })
            
    num_participants = len(participating_list)
    if num_participants == 0:
        raise RuntimeError(f'No input available for output at: {out_time}')

    #initialize advection code
    extrapolate = pysteps.extrapolation.interface.get_method("semilagrangian")

    for pp, item in enumerate(participating_list):

        # get wind
        wind_file = args.motion_vectors_dir + item['vtime'].strftime('%Y%m%d%H%M_end_window.npz')
        try:
            raw_mv = np.load(wind_file)
        except:
            raise RuntimeError(f'Problem loading file: {wind_file}')

        # first time around, init output matrices
        if pp == 0:
            ny, nx = raw_mv['uv_motion'][0,:,:].shape   # note ny,nx order ; mv data is already transposed
            rr_arr      = np.full((nx, ny, num_participants), np.nan)
            qi_arr      = np.full((nx, ny, num_participants), np.nan)
            weights_arr = np.full((nx, ny, num_participants), np.nan)

        # scale motion vectors
        scaling_factor = item['dt'] / args.input_dt
        uv_scaled = _scale_wind(raw_mv['uv_motion'], scaling_factor)

        # get precip and qi to extrapolate
        fst_file = args.processed_dir+item['vtime'].strftime(args.output_file_struc)
         
        fst_entry = fst_tools.get_data(file_name=fst_file, var_name='RDPR', datev=item['vtime'])
        if fst_entry is None:
            raise ValueError(f"Unable to get source precip in {fst_file}, at {item['vtime']}")
        else:
            precip_rate = np.transpose(fst_entry['values'])
            np.where(precip_rate < 0., 0., precip_rate) #change -9999. to 0. for less problems when advecting
         
        fst_entry = fst_tools.get_data(file_name=fst_file, var_name='RDQI', datev=item['vtime'])
        if fst_entry is None:
            raise ValueError(f"Unable to get source quality index in {fst_file}, at {item['vtime']}")
        else:
            quality_index = np.transpose(fst_entry['values'])

        # advect wind at appropriate time
        advected_precip  = np.squeeze(np.transpose(extrapolate(precip_rate,   uv_scaled, 1, outval=np.nan)))
        advected_quality = np.squeeze(np.transpose(extrapolate(quality_index, uv_scaled, 1, outval=np.nan)))
         
        # save in output array and 
        # adjust missing values that could have been modified during advection
        rr_arr[:,:,pp] = np.where(advected_precip  < 0., np.nan, advected_precip)
        qi_arr[:,:,pp] = np.where(advected_quality < 0., np.nan, advected_quality)
        weights_arr[:,:,pp] = item['weight']*qi_arr[:,:,pp]


    #normalize weight so that column sum is one everywhere
    norm_factor = np.nansum(weights_arr, axis=2, keepdims=True)
    weights_arr /= norm_factor
    norm_factor = np.squeeze(norm_factor)   # we don't need the 3rd dim of size 1 anymore

    interpolated_precip  = np.nansum(weights_arr * rr_arr, axis=2)
    interpolated_quality = np.nansum(weights_arr * qi_arr, axis=2)
    
    # replace missing values where no data was availablet st
    interpolated_precip  = np.where(np.isclose(norm_factor, 0.), missing, interpolated_precip)
    interpolated_quality = np.where(np.isclose(norm_factor, 0.), 0.,      interpolated_quality)


    etiket = 'EXTRAPOL'
    _write_fst_file(out_time, interpolated_precip, interpolated_quality, args, etiket=etiket, 
                   output_file=fst_output_file)

    #make a figure for this std file if the argument figure_dir was provided
    if args.figure_dir is not None:
        radar_tools.plot_rdpr_rdqi(fst_file=fst_output_file, 
                                   this_date=out_time,
                                   args=args)

    logger.info(f'Done interpolating to: {out_time}')
    return np.array([1], dtype=float)

def _nowcast_t_interp(args):
    """ given precip estimates and motion vectors, do nowcasts as a mean of time interpolation

    """

    import time
    import glob
    import signal
    import dask
    import dask.array as da
    from domcmc import fst_tools

    #logging
    logger = _setup_logging(args)


    if args.ncores == 1 :
        #serial execution
        logger.info('Launching SERIAL computation of nowcast time interpolation')

        for out_time in args.output_date_list:
            _t_interp_at_one_time(out_time, args)
    else:
        #parallel execution
        logger.info('Launching PARALLEL of nowcast time interpolation')

        tt1 = time.time()

        #delay input data 
        delayed_args = dask.delayed(args)

        #delayed list of results
        res_list = [dask.array.from_delayed(_dask_t_interp_at_one_time(this_date, delayed_args), (1,), float) for this_date in args.output_date_list ]

        #what output do we want
        res_stack = dask.array.stack(res_list)
                
        # computation happens here
        big_arr = res_stack.compute()


        tt2 = time.time()
        logger.info(f'Parallel execution done; walltime: {tt2-tt1} seconds')


def _aquire_lock(filename):
    """ Acquire file lock
    """
    import time
    import os

    lock_file = filename+'.lock'

    start_time = time.time()
    timeout = 120.
    delay   = .5
    while True:
        try:
            # Attempt to create the lockfile.
            # These flags cause os.open to raise an OSError if the file already exists.
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(fd, "a") as f:
                f.write('You shall not pass!')
            break
        except FileExistsError:
            if (time.time() - start_time) >= timeout:
                raise RuntimeError("Timeout occurred getting file lock")
            print(f'waiting lockfile: {lock_file}')
            time.sleep(delay)

    return True


def _release_lock(filename):
    import os
    lock_file = filename+'.lock'
    if os.path.isfile(lock_file):
        os.unlink(lock_file)



def _write_fst_file(out_date, precip_rate, quality_index, args, etiket='', 
                    output_file=None):
    """ write precip to a fst file
    """
    import os
    import copy
    import rpnpy.librmn.all as rmn
    from rpnpy.rpndate import RPNDate
    from domcmc import fst_tools

    logger = _setup_logging(args)

    # read fst template
    fst_template = fst_tools.get_data(args.sample_pr_file, var_name='PR')

    #prepare std objects
    #cmc timestamp
    date_obj = RPNDate(out_date)
    cmc_timestamp = date_obj.datev

    #fst file to write to
    if output_file is None:
        output_file = args.output_dir + out_date.strftime(args.output_file_struc)

    # aquire lock for this file
    _aquire_lock(output_file)

    first_time_writing_to_outfile = True
    if os.path.isfile(output_file):
        first_time_writing_to_outfile = False

    logger.info(f'writing {output_file}')
    iunit = rmn.fstopenall(output_file,rmn.FST_RW)

    if 'combined_yy_grid' in fst_template.keys():
        #write ^> to output file
        if first_time_writing_to_outfile:
            rmn.writeGrid(iunit, fst_template['combined_yy_grid'])

        #put precip rate into combined YY array
        nx, ny = precip_rate.shape
        reshaped_pr = np.full((nx, ny*2), -9999.)
        reshaped_qi = np.zeros((nx, ny*2))
        reshaped_pr[:,0:ny] = precip_rate
        reshaped_qi[:,0:ny] = quality_index
        precip_rate   = reshaped_pr
        quality_index = reshaped_qi

    else:
        #write >> ^^ to output file
        if first_time_writing_to_outfile:
            rmn.writeGrid(iunit, fst_template['grid'])

    #make RDPR entry
    rdpr_entry = copy.deepcopy(fst_template['meta'])
    rdpr_entry['nomvar']        = 'RDPR'
    rdpr_entry['etiket']        = etiket
    rdpr_entry['dateo']         = cmc_timestamp
    rdpr_entry['datev']         = cmc_timestamp
    rdpr_entry['ip2']           = 0
    rdpr_entry['deet']          = 0
    rdpr_entry['npas']          = 0
    rdpr_entry['nbits']         = 32
    rdpr_entry['typvar']        = 'I'
    rdpr_entry['d']             = np.asfortranarray(precip_rate, dtype='float32')

    #make RDQI entry
    rdqi_entry = copy.deepcopy(fst_template['meta'])
    rdqi_entry['nomvar']        = 'RDQI'
    rdqi_entry['etiket']        = etiket
    rdqi_entry['dateo']         = cmc_timestamp
    rdqi_entry['datev']         = cmc_timestamp
    rdqi_entry['ip2']           = 0
    rdqi_entry['deet']          = 0
    rdqi_entry['npas']          = 0
    rdqi_entry['nbits']         = 32
    rdqi_entry['typvar']        = 'I'
    rdqi_entry['d']             = np.asfortranarray(quality_index, dtype='float32')

    rmn.fstecr(iunit, rdpr_entry, rewrite=False)
    rmn.fstecr(iunit, rdqi_entry, rewrite=False)

    #close file
    rmn.fstcloseall(iunit)

    # release lock on file to allow other processes to write to it
    _release_lock(output_file)

    logger.info(f'Done writing {output_file}')




def obs_process(args=None):
    """ Batch processing of precipitation observation

      This function provides time and space interpolation of precipitiaton data.
      If desired, processing such as median filtering and boxcar-type filtering
      can be applied at the same time. 

      See  for an example.

      It is intended to be used as a command-line program, that would be called like:

      .. code-block:: bash

          python -m domutils.radar_tools.obs_process
                    --input_data_dir    /space/hall4/sitestore/eccc/mrd/rpndat/dja001/data/radar_h5_composites/v8/ 
                    --output_dir        /home/dja001/python/obs_process/outdir/                       
                    --output_figure_dir /home/dja001/python/obs_process/figdir/                       
                    --fst_file_struc    %Y/%m/%d/%Y%m%d%H%M_mosaic.fst                                             
                    --input_file_struc  %Y/%m/%d/qcomp_%Y%m%d%H%M.h5                                               
                    --h5_latlon_file    /home/dja001/shared_stuff/files/radar_continental_2.5km_2882x2032.pickle   
                    --t0                ${t_start}                                                                 
                    --tf                ${t_stop}                                                                  
                    --input_dt          10                                                                         
                    --sample_pr_file    /space/hall4/sitestore/eccc/mrd/rpndat/dja001/domains/hrdps_5p1_prp0.fst   
                    --ncores            40                                                                         
                    --complete_dataset  True                                                                       
                    --median_filt       3                                                                          
                    --smooth_radius     4                                                                          

      Alternatively, is is possible to call this function directly from Python by defining a
      simple object whose attributes are the arguments.
      Such use is demonstrated in :ref:`Interpolate a batch of observations`.
      

      Argument description:

      .. argparse::
          :module: domutils.radar_tools.obs_process
          :func: _define_parser
          :prog: obs_process


          

    """



    import os
    import sys
    import time
    import datetime
    import domutils._py_tools as dpy


    #keep track of runtime
    time_start = time.time()

    if args is None:

        # we are processing command line arguments
        parser = _define_parser()
        args = parser.parse_args()

    else:
        # arguments are passed through the attributes of an object, 
        # set all non mandatory arguments to default values when called with args
        non_mandatory_args = _define_parser(only_arg_list=True)
        for (var_name, vtype, vdefault, vhelp) in non_mandatory_args:
            # remove the -- at the beginning  var_name
            var_name = var_name[2:]
            if not hasattr(args, var_name):
                setattr(args, var_name, vdefault)


    #add trailling / to all directories
    args.input_data_dir += '/'
    args.output_dir += '/'

    if args.output_figure_dir == 'no_figures' or args.output_figure_dir == 'None':
        args.output_figure_dir = None
    else:
        args.output_figure_dir += '/'

    if args.processed_figure_dir == 'no_figures' or args.processed_figure_dir == 'None':
        args.processed_figure_dir = None
    else:
        args.processed_figure_dir += '/'

    if args.intermediate_files_dir == 'None':
        args.intermediate_files_dir = None
    else:
        args.intermediate_files_dir += '/'

    # parse numeric inputs
    if args.accum_len is not None :
        if args.accum_len == 'None' :
            args.accum_len = None
        else:
            args.accum_len = _parse_num(args.accum_len)

    if args.median_filt is not None :
        if args.median_filt == 'None' :
            args.median_filt = None
        else:
            args.median_filt = _parse_num(args.median_filt)

    if args.median_filt_before_mv is not None :
        if args.median_filt_before_mv == 'None' :
            args.median_filt_before_mv = None
        else:
            args.median_filt_before_mv = _parse_num(args.median_filt_before_mv)

    if args.smooth_radius is not None :
        if args.smooth_radius == 'None' :
            args.smooth_radius = None
        else:
            args.smooth_radius = _parse_num(args.smooth_radius)

    if args.cartopy_dir == 'None':
        #if argument is not provided do nothing
        args.cartopy_dir = None
    else:
        #setup directory where shapefiles will be found
        args.cartopy_dir += '/'

    if args.h5_latlon_file == 'None':
        args.h5_latlon_file = None

    #parse complete_dataset
    #not directly using bool type since any string, including 'False', will be interpreted as True....
    if args.complete_dataset.lower() == 'false':
        args.complete_dataset = False
    elif args.complete_dataset.lower() == 'true':
        args.complete_dataset = True
    else :
        raise ValueError('Argument --complete_dataset can only be set to True or False')

    #change date from string to datetime object
    args.input_t0 = _to_datetime(args.input_t0)
    if args.input_tf is not None:
        #if input_tf provided use it
        args.input_tf = _to_datetime(args.input_tf)
    else:
        #otherwise get it from fcst len
        args.fcst_len = _parse_num(args.fcst_len)
        args.input_tf = args.input_t0 + datetime.timedelta(seconds=args.fcst_len*3600.)

    args.input_dt = _parse_num(args.input_dt, dtype='float', deltat_seconds=True)

    if args.interp_max_dt is None:
        args.interp_max_dt = args.input_dt 
    elif args.interp_max_dt == 'None':
        args.interp_max_dt = args.input_dt 
    else:
        args.interp_max_dt = _parse_num(args.interp_max_dt, dtype='float', deltat_seconds=True)

    #output dates
    if args.output_t0 is None:
        args.output_t0 = args.input_t0
    elif args.output_t0 == 'None':
        args.output_t0 = args.input_t0
    else:
        args.output_t0 = _to_datetime(args.output_t0)
    if args.output_tf is None:
        args.output_tf = args.input_tf
    elif args.output_tf == 'None':
        args.output_tf = args.input_tf
    else:
        args.output_tf = _to_datetime(args.output_tf)
    if args.output_dt is None:
        args.output_dt = args.input_dt
    elif args.output_dt == 'None':
        args.output_dt = args.input_dt
    else: 
        args.output_dt = _parse_num(args.output_dt, dtype='float', deltat_seconds=True)

    if args.tinterpolated_file_struc is None:
        args.tinterpolated_file_struc = args.output_file_struc
    elif args.tinterpolated_file_struc == 'None':
        args.tinterpolated_file_struc = args.output_file_struc

    # flush logs directory if it exists
    if os.path.isdir('logs'):
        os.system('rm -rf logs')
    
    # initialize logging
    logger = _setup_logging(args)

    #log header
    logger.info('')
    logger.info('')
    logger.info('executing python script:  domutils.radar_tools.obs_process.py')
    logger.info('All logs printed to stdout can also be found in ./logs/')
    logger.info('')
    logger.info('')

    logger.info('After parsing, input arguments are:')
    for arg in vars(args):
       logger.info(arg +' = '+ str(getattr(args, arg)))
    logger.info('')
    logger.info('')

    #make list of dates where input radar data is needed and add it to arguments
    t_len = (args.input_tf-args.input_t0) + datetime.timedelta(seconds=1)    #+ 1 second for inclusive end point
    elasped_seconds = t_len.days*3600.*24. + t_len.seconds
    args.input_date_list = [args.input_t0 + datetime.timedelta(seconds=x) for x in np.arange(0,elasped_seconds,args.input_dt)]

    #make list of dates where output radar data is desired
    t_len = (args.output_tf-args.output_t0) + datetime.timedelta(seconds=1)    #+ 1 second for inclusive end point
    elasped_seconds = t_len.days*3600.*24. + t_len.seconds
    args.output_date_list = [args.output_t0 + datetime.timedelta(seconds=x) for x in np.arange(0,elasped_seconds,args.output_dt)]


    if args.complete_dataset:
        logger.info('With the complete_dataset=True option, no cleanup of directories is performed. ')
        logger.info('Dangling lock files from a previously aborted run could be an issue.')
    else:
        # if complete dataset is set, we leave everything to be completed

        # If time interpolated outputs already exists for this period, 
        # they are deleted here along with potentially dangling lock files
        out_file_list = set( [args.output_dir + out_date.strftime(args.tinterpolated_file_struc) for out_date in args.output_date_list] )
        for this_file in sorted(out_file_list):
            if os.path.isfile(this_file):
                os.remove(this_file)
            if os.path.isfile(this_file+'.lock'):
                os.remove(this_file+'.lock')


    if args.ncores > 1:
        logger.info(f'Starting local dask cluster with {args.ncores} workers.')

        tmp_dir = os.environ['BIG_TMPDIR']
        dask_client = dask.distributed.Client(processes=True, threads_per_worker=1, 
                                              n_workers=args.ncores, 
                                              local_directory=tmp_dir, 
                                              silence_logs=40) 
        logger.info('Done')
    else:
        dask_client=None

    # time interpolation 
    if args.t_interp_method == 'None':
        # No temporal interpolation, only observation processing
        if args.output_date_list != args.input_date_list:
            raise ValueError('Select a time interpolation method if output is desired at times different from input')
        else:
            # 1- process radar file and write output to new files
            args.figure_dir = args.output_figure_dir 
            _process_a_bunch_of_times(args)

    elif args.t_interp_method == 'nowcast':

        # three precip maps are needed for computation of wind vectors, input data must then be available 
        # before the earliers output time
        if args.output_date_list[0] < args.input_date_list[2] :
            raise ValueError('For nowcast interpolation the earliest output time must be at least two input delta_t after the earliest input time')

        # don't comment these
        #
        # save final output destination for use after override
        final_output_dir = args.output_dir 
        # set tmp files dir is specified
        if args.intermediate_files_dir is not None:
            intermediate_files_dir  = args.intermediate_files_dir
        else:
            intermediate_files_dir  = final_output_dir
        # directories for temporary files
        args.processed_dir      = os.path.join(intermediate_files_dir, 'processed/')
        args.motion_vectors_dir = os.path.join(intermediate_files_dir, 'motion_vectors/')


        # 1- Process input data and write output to files
        #    override output dir since in this context these outputs are only intermediate results
        args.output_dir = args.processed_dir
        args.figure_dir = args.processed_figure_dir 
        _process_a_bunch_of_times(args)

        # 2- compute motion vectors associated with processed outputs computed at the step above
        _make_motion_vectors(args)

        # 3- use nowcast for time interpolation
        args.output_dir = final_output_dir 
        args.figure_dir = args.output_figure_dir 
        _nowcast_t_interp(args)

    else:
        raise ValueError('type of time interpolation not supported.')

    dask_client.close()

    #we are done
    time_stop = time.time()
    logger.info(f'Python code completed, Runtime was : {time_stop-time_start} seconds')


def _define_parser(only_arg_list=False):
    '''return argument parser
    '''
    import argparse

    non_mandatory_args = [
          ('--tinterpolated_file_struc', str,   'None',      "strftime syntax for constructing time interpolated filenames"),
          ('--h5_latlon_file'          , str,   'None',      "Pickle file containing the lat/lons of the Baltrad grid"),
          ('--input_tf'                , str,   'None',      "yyyymmsshhmmss end      time; datestring"),
          ('--fcst_len'                , str,   'None',      "duration of forecast (hours)"),
          ('--accum_len'               , str,   'None',      "duration of accumulation (minutes)"),
          ('--output_t0'               , str,   'None',      "yyyymmsshhmmss begining time; datestring"),
          ('--output_tf'               , str,   'None',      "yyyymmsshhmmss end      time; datestring"),
          ('--output_dt'               , str,   'None',      "interval (minutes M or seconds S) between output radar mosaics"),
          ('--t_interp_method'         , str,   'None',      "time interpolation method"),
          ('--interp_max_dt'           , str,   'None',      "max interval (minutes M or seconds S) where data will contribute to nowcast interpolation"),
          ('--sample_pr_file'          , str,   'None',      "File containing PR to establish the domain"),
          ('--output_file_format'      , str,   'npz',       "File format of processed files"),
          ('--ncores'                  , int,   1,           "number of cores for parallel execution"),
          ('--complete_dataset'        , str,   'False',     "Skip existing files, default is to clobber them"),
          ('--median_filt'             , str,   'None',      "box size (pixels) for median filter"),
          ('--median_filt_before_mv'   , str,   'None',      "Apply median filter on reflectivity before computing motion vectors"),
          ('--smooth_radius'           , str,   'None',      "radius (km) where radar data be smoothed"),
          ('--intermediate_files_dir'  , str,   'None',      "Where to store processed and motion vector files when doing nowcast interpolation"),
          ('--processed_figure_dir'    , str,   'no_figures',"If a path is provided, a figure will be created for each std file created"),
          ('--output_figure_dir'       , str,   'no_figures',"If a path is provided, a figure will be created for each std file created"),
          ('--cartopy_dir'             , str,   'None',      "Directory for cartopy shape files"),
          ('--figure_format'           , str,   'gif',       "File format of figure "),
          ('--log_level'               , str,   'INFO',      "minimum level of messages printed to stdout and in log files "),
          ]

    if only_arg_list:
        # we only want the list of mandatory arguments and their default values
        return non_mandatory_args
    else:
        # we want a full fledged argument parser
        desc="read radar H5 files, interpolate/smooth and write to FST"
        parser = argparse.ArgumentParser(description=desc, 
                 prefix_chars='-+', formatter_class=argparse.RawDescriptionHelpFormatter)
        required_args = parser.add_argument_group('Required named arguments')
        required_args.add_argument("--input_data_dir"   , type=str,   required=True,  help="path of source radar mosaics files")
        required_args.add_argument("--input_t0"         , type=str,   required=True,  help="yyyymmsshhmmss begining time; datestring")
        required_args.add_argument("--input_dt"         , type=str,   required=True,  help="interval (minutes M or seconds S) between input radar mosaics")
        required_args.add_argument("--output_dir"       , type=str,   required=True,  help="directory for output fst files")
        required_args.add_argument("--output_file_struc", type=str,   required=True,  help="strftime syntax for constructing fst filenames for output of obsprocess")
        required_args.add_argument("--input_file_struc" , type=str,   required=True,  help="strftime syntax for constructing H5  filenames")

        optional_args = parser.add_argument_group('Optional named arguments')
        for (var_name, vtype, vdefault, vhelp) in non_mandatory_args:
            optional_args.add_argument(var_name, type=vtype, default=vdefault, help=vhelp)

        return parser

if __name__ == '__main__':     
    obs_process()

