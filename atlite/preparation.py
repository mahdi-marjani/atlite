import xarray as xr
import pandas as pd
import os, shutil
import filelock
import logging
from six import itervalues
from six.moves import map
from multiprocessing import Pool

logger = logging.getLogger(__name__)

def cutout_do_task(task, write_to_file=True):
    task = task.copy()
    prepare_func = task.pop('prepare_func')
    if write_to_file:
        datasetfns = task.pop('datasetfns')

    try:
        data = prepare_func(**task)
        if data is None:
            data = []

        if write_to_file:
            for yearmonth, ds in data:
                fn = datasetfns[yearmonth]
                ds = ds.load() # Don't loose time waiting for the lock, but increases the mem consumption to just about 2gb

                if write_to_file:
                    with filelock.SoftFileLock(fn + '.lock'):
                        ds.to_netcdf(fn, mode='a')
                    logger.debug("Appended variable(s) %s to %s generated by %s",
                                 ", ".join('`' + x + '`' for x in ds.data_vars),
                                 os.path.basename(fn),
                                 prepare_func.__name__)
    except Exception as e:
        logger.exception("Exception occured in the task with prepare_func `%s`: %s",
                         prepare_func.__name__, e.args[0])
        raise e

    if not write_to_file:
        return data

def cutout_prepare(cutout, overwrite=False):
    if cutout.prepared and not overwrite:
        raise ArgumentError("The cutout is already prepared. If you want to recalculate it, "
                            "anyway, then you must supply an `overwrite=True` argument.")

    logger.info("Starting preparation of cutout '%s'", cutout.name)

    cutout_dir = cutout.cutout_dir
    yearmonths = cutout.coords['year-month'].to_index()
    lons = cutout.coords['lon']
    lats = cutout.coords['lat']

    # Delete cutout_dir
    if os.path.isdir(cutout_dir):
        logger.debug("Deleting cutout_dir '%s'", cutout_dir)
        shutil.rmtree(cutout_dir)

    logger.debug("Creating empty netcdf files for all months in '%s'", cutout_dir)
    # Create all datasets beforehand
    datasetfns = {ym: cutout.datasetfn(ym) for ym in [None] + yearmonths.tolist()}
    os.mkdir(cutout_dir)
    cutout.meta.unstack('year-month').to_netcdf(datasetfns[None])
    for ym in yearmonths:
        xr.Dataset().to_netcdf(datasetfns[ym])

    # Compute data and fill files
    tasks = []
    for series in itervalues(cutout.weather_data_config):
        series = series.copy()
        series['meta_attrs'] = cutout.meta.attrs
        tasks_func = series.pop('tasks_func')
        tasks += tasks_func(lons=lons, lats=lats, yearmonths=yearmonths, **series)
    for t in tasks:
        t['datasetfns'] = datasetfns

    logger.info("%d tasks have been collected. Starting running them on %s.",
                len(tasks),
                ("%d processes" % cutout.nprocesses)
                if cutout.nprocesses is not None
                else "all processors")

    pool = Pool(processes=cutout.nprocesses)
    try:
        pool.map(cutout_do_task, tasks)
    except Exception as e:
        pool.terminate()
        logger.info("Preparation of cutout '%s' has been interrupted by an exception. "
                    "Purging the incomplete cutout_dir.",
                    cutout.name)
        shutil.rmtree(cutout_dir)
        raise e
    pool.close()
    logger.info("Cutout '%s' has been successfully prepared", cutout.name)
    cutout.prepared = True

def cutout_produce_specific_dataseries(cutout, yearmonth, series_name):
    lons = cutout.coords['lon']
    lats = cutout.coords['lat']
    series = cutout.weather_data_config[series_name].copy()
    series['meta_attrs'] = cutout.meta.attrs
    tasks_func = series.pop('tasks_func')
    tasks = tasks_func(lons=lons, lats=lats, yearmonths=[yearmonth], **series)

    assert len(tasks) == 1
    data = cutout_do_task(tasks[0], write_to_file=False)
    assert len(data) == 1 and data[0][0] == yearmonth
    return data[0][1]

def cutout_get_meta(cutout, lons, lats, years, months=None, **dataset_params):
    if months is None:
        months = slice(1, 12)
    meta_kwds = cutout.meta_data_config.copy()
    meta_kwds.update(dataset_params)

    prepare_func = meta_kwds.pop('prepare_func')
    ds = prepare_func(lons=lons, lats=lats, year=years.stop, month=months.stop, **meta_kwds)
    ds.attrs.update(dataset_params)

    start, second, end = map(pd.Timestamp, ds.coords['time'].values[[0, 1, -1]])
    month_start = pd.Timestamp("{}-{}".format(years.stop, months.stop))

    offset_start = (start - month_start)
    offset_end = (end - (month_start + pd.offsets.MonthBegin()))
    step = (second - start).components.hours

    ds.coords["time"] = pd.date_range(
        start=pd.Timestamp("{}-{}".format(years.start, months.start)) + offset_start,
        end=(month_start + pd.offsets.MonthBegin() + offset_end),
        freq='h' if step == 1 else ('%dh' % step))

    ds.coords["year"] = range(years.start, years.stop+1)
    ds.coords["month"] = range(months.start, months.stop+1)
    ds = ds.stack(**{'year-month': ('year', 'month')})

    return ds
