# stdlib imports
from configobj import ConfigObj
import os
import shutil
import numpy as np
import tempfile
import urllib
import re
from argparse import Namespace

# local imports
from mapio.shake import getHeaderData
from mapio.gdal import GDALGrid
from impactutils.io.cmd import get_command_output
from mapio.shake import ShakeGrid
from gfail.conf import correct_config_filepaths
import gfail.logisticmodel as LM
from gfail.godt import godt2008
from gfail.makemaps import (modelMap, interactiveMap, GFSummary)
from gfail.webpage import hazdev
from gfail.utilities import (
    get_event_comcat, parseConfigLayers,
    parseMapConfig, text_to_json, write_floats,
    savelayers)


def run_gfail(args):
    """Runs ground failure.

    Args:
        args: dictionary or argument parser Namespace output by bin/gfail
            program.

    Returns:
        list: Names of created files.

    """
    # TODO: ADD CONFIG VALIDATION STEP THAT MAKES SURE ALL THE FILES EXIST
    filenames = []
    # If args is a dictionary, convert to a Namespace
    if isinstance(args, dict):
        args = Namespace(**args)

    if args.set_default_paths:
        set_default_paths(args)
        print('default paths set, continuing...\n')

    if args.list_default_paths:
        list_default_paths()
        return

    if args.reset_default_paths:
        reset_default_paths()
        return

    if args.make_webpage:
        # Turn on GIS and HDF5 flags
        gis = True
        hdf5 = True
    else:
        gis = args.gis
        hdf5 = args.hdf5

    # Figure out what models will be run
    if args.shakefile is not None:  # user intends to actually run some models
        shakefile = args.shakefile

        # make output location for things
        if args.output_filepath is None:
            outdir = os.getcwd()
        else:
            outdir = args.output_filepath

        if (hdf5 or args.make_static_pngs or
                args.make_static_pdfs or
                args.make_interactive_plots or
                gis):
            if not os.path.exists(outdir):
                os.makedirs(outdir)

        # download if is url
        # cleanup = False
        if not os.path.isfile(shakefile):
            if isURL(shakefile):
                # getGridURL returns a named temporary file object
                shakefile = getGridURL(shakefile)
                # cleanup = True  # Be sure to delete it after
            else:
                raise NameError('Could not find "%s" as a file or a valid url'
                                % (shakefile))
        eventid = getHeaderData(shakefile)[0]['event_id']

        # Get entire path so won't break if running gfail with relative path
        shakefile = os.path.abspath(shakefile)

        if args.extract_contents:
            outfolder = outdir
        else:  # Nest in a folder named by eventid
            outfolder = os.path.join(outdir, eventid)
            if not os.path.exists(outfolder):
                os.makedirs(outfolder)

        # Copy shake grid into output directory
        # --- this is base on advice from Mike that when running in production
        #     the shake grids are not archived and so if we need/want to have
        #     the exact grid used for the calculation later if there's every a
        #     question about how the calculation was done, the safest thing is
        #     to store a copy of it here.
        shake_copy = os.path.join(outfolder, "grid.xml")
        shutil.copyfile(shakefile, shake_copy)

        # Write shakefile to a file for use later
        shakename = os.path.join(outfolder, "shakefile.txt")
        shake_file = open(shakename, "wt")
        shake_file.write(shake_copy)
        shake_file.close()
        filenames.append(shakename)

        config = args.config

        if args.config_filepath is not None:
            # only add config_filepath if full filepath not given and file
            # ext is .ini
            if (not os.path.isabs(config) and
                    os.path.splitext(config)[-1] == '.ini'):
                config = os.path.join(args.config_filepath, config)

        if os.path.splitext(config)[-1] == '.ini':
            temp = ConfigObj(config)
            if len(temp) == 0:
                raise Exception(
                    'Could not find specified .ini file: %s' % config)
            if args.data_path is not None:
                temp = correct_config_filepaths(args.data_path, temp)
            configs = [temp]
            conffail = []
        else:
            # input is a list of config files
            f = open(config, 'r')
            configlist = f.readlines()
            configs = []
            conffail = []
            for conf in configlist:
                conf = conf.strip()
                if not os.path.isabs(conf):
                    # only add config_filepath if full filepath not given
                    conf = os.path.join(args.config_filepath, conf)
                try:
                    temp = ConfigObj(conf)
                    if temp:
                        if args.data_path is not None:
                            temp = correct_config_filepaths(
                                args.data_path, temp)
                        configs.append(temp)
                    else:
                        conffail.append(conf)
                except:
                    conffail.append(conf)

        print('\nRunning the following models:')

        for conf in configs:
            print('\t%s' % conf.keys()[0])
        if len(conffail) > 0:
            print('Could not find or read in the following config files:\n')
            for conf in conffail:
                print('\t%s' % conf)
            print('\nContinuing...\n')

        if args.set_bounds is not None:
            if 'zoom' in args.set_bounds:
                temp = args.set_bounds.split(',')
                print('Using %s threshold of %1.1f to cut model bounds'
                      % (temp[1].strip(), float(temp[2].strip())))
                bounds = get_bounds(shakefile, temp[1].strip(),
                                    float(temp[2].strip()))
            else:
                temp = eval(args.set_bounds)
                latmin = temp[0]
                latmax = temp[1]
                lonmin = temp[2]
                lonmax = temp[3]
                bounds = {'xmin': lonmin, 'xmax': lonmax,
                          'ymin': latmin, 'ymax': latmax}
            print('Applying bounds of lonmin %1.2f, lonmax %1.2f, '
                  'latmin %1.2f, latmax %1.2f'
                  % (bounds['xmin'], bounds['xmax'],
                     bounds['ymin'], bounds['ymax']))
        else:
            bounds = None

        if args.make_webpage or args.make_summary:
            results = []

        # pre-read in ocean trimming file polygons so only do this step once
        if args.trimfile is not None:
            if not os.path.exists(args.trimfile):
                print('trimfile defined does not exist: %s\n'
                      'Ocean will not be trimmed.' % args.trimfile)
                trimfile = None
            elif os.path.splitext(args.trimfile)[1] != '.shp':
                print('trimfile must be a shapefile, '
                      'ocean will not be trimmed')
                trimfile = None
            else:
                trimfile = args.trimfile
        else:
            trimfile = None

        # Get finite fault ready, if exists

        ffault = None
        point = True
        if args.finite_fault is not None:
            point = False
            try:
                if os.path.splitext(args.finite_fault)[-1] == '.txt':
                    ffault = text_to_json(args.finite_fault)
                elif os.path.splitext(args.finite_fault)[-1] == '.json':
                    ffault = args.finite_fault
                else:
                    print('Could not read in finite fault, will '
                          'try to download from comcat')
                    ffault = None
            except:
                print('Could not read in finite fault, will try to '
                      'download from comcat')
                ffault = None

        if ffault is None:
            # Try to get finite fault file, if it exists
            try:
                returned_ev = get_event_comcat(shakefile)
                if returned_ev is not None:
                    testjd, detail, temp = returned_ev
                    if 'faultfiles' in testjd['input']['event_information']:
                        ffilename = testjd['input']['event_information']['faultfiles']
                        if len(ffilename) > 0:
                            # Download the file
                            with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
                                temp.getContent(ffilename, filename=f.name)
                                ffault = text_to_json(f.name)
                                os.remove(f.name)
                            point = False
                        else:
                            point = True
                else:
                    print('Unable to determine source type, unknown if finite'
                          ' fault or point source')
                    ffault = None
                    point = False

            except Exception as e:
                print(e)
                print('Unable to determine source type, unknown if finite'
                      ' fault or point source')
                ffault = None
                point = False

        # Loop over config files
        for conf in configs:
            modelname = conf.keys()[0]
            print('\nNow running %s:' % modelname)
            modelfunc = conf[modelname]['funcname']
            if modelfunc == 'LogisticModel':
                lm = LM.LogisticModel(shakefile, conf,
                                      uncertfile=args.uncertfile,
                                      saveinputs=args.save_inputs,
                                      bounds=bounds,
                                      numstd=float(args.std),
                                      trimfile=trimfile)

                maplayers = lm.calculate()
            elif modelfunc == 'godt2008':
                maplayers = godt2008(shakefile, conf,
                                     uncertfile=args.uncertfile,
                                     saveinputs=args.save_inputs,
                                     bounds=bounds,
                                     numstd=float(args.std),
                                     trimfile=trimfile)
            else:
                print('Unknown model function specified in config for %s '
                      'model, skipping to next config' % modelfunc)
                continue

            # time1 = datetime.datetime.utcnow().strftime('%d%b%Y_%H%M')
            # filename = ('%s_%s_%s' % (eventid, modelname, time1))

            if args.appendname is not None:
                filename = ('%s_%s_%s' % (eventid, modelname, args.appendname))
            else:
                filename = ('%s_%s' % (eventid, modelname))
            if hdf5:
                filenameh = filename + '.hdf5'
                if os.path.exists(filenameh):
                    os.remove(filenameh)
                savelayers(maplayers, os.path.join(outfolder, filenameh))
                filenames.append(filenameh)

            if args.make_static_pdfs or args.make_static_pngs:
                plotorder, logscale, lims, colormaps, maskthreshes = \
                    parseConfigLayers(maplayers, conf)
                mapconfig = ConfigObj(args.mapconfig)

                kwargs = parseMapConfig(
                    mapconfig, fileext=args.mapdata_filepath)
                junk, filenames1 = modelMap(
                    maplayers, shakefile,
                    suptitle=conf[modelname]['shortref'],
                    boundaries=None,
                    zthresh=0.,
                    lims=lims,
                    plotorder=plotorder,
                    maskthreshes=maskthreshes,
                    maproads=False,
                    mapcities=True,
                    colormaps=colormaps,
                    savepdf=args.make_static_pdfs,
                    savepng=args.make_static_pngs,
                    printparam=True,
                    inventory_shapefile=None,
                    outputdir=outfolder,
                    outfilename=filename,
                    scaletype='continuous',
                    logscale=logscale, **kwargs)
                for filen in filenames1:
                    filenames.append(filen)

                # make model only plots too
                if len(maplayers) > 1:
                    plotorder, logscale, lims, colormaps, maskthreshes = \
                        parseConfigLayers(maplayers, conf, keys=['model'])
                    junk, filenames1 = modelMap(
                        maplayers, shakefile,
                        suptitle=conf[modelname]['shortref'], boundaries=None,
                        zthresh=0., lims=lims, plotorder=plotorder,
                        maskthreshes=maskthreshes, maproads=False,
                        mapcities=True, savepdf=args.make_static_pdfs,
                        savepng=args.make_static_pngs, printparam=True,
                        inventory_shapefile=None, outputdir=outfolder,
                        outfilename=filename + '-just_model',
                        colormaps=colormaps, scaletype='continuous',
                        logscale=logscale, **kwargs)
                    for filen in filenames1:
                        filenames.append(filen)
            if args.make_interactive_plots:
                plotorder, logscale, lims, colormaps, maskthreshes = \
                    parseConfigLayers(maplayers, conf)
                junk, filenames1 = interactiveMap(
                    maplayers, plotorder=plotorder, shakefile=shakefile,
                    inventory_shapefile=None, maskthreshes=maskthreshes,
                    colormaps=colormaps, isScenario=False,
                    scaletype='continuous', lims=lims, logscale=logscale,
                    ALPHA=0.7, outputdir=outfolder, outfilename=filename,
                    tiletype='Stamen Terrain', separate=True,
                    faultfile=ffault)
                for filen in filenames1:
                    filenames.append(filen)
            if gis:

                for key in maplayers:
                    # Get simplified name of key for file naming
                    RIDOF = '[+-]?(?=\d*[.eE])(?=\.?\d)'\
                            '\d*\.?\d*(?:[eE][+-]?\d+)?'
                    OPERATORPAT = '[\+\-\*\/]*'
                    keyS = re.sub(OPERATORPAT, '', key)
                    # remove floating point numbers
                    keyS = re.sub(RIDOF, '', keyS)
                    # remove parentheses
                    keyS = re.sub('[()]*', '', keyS)
                    # remove any blank spaces
                    keyS = keyS.replace(' ', '')
                    filen = os.path.join(outfolder, '%s_%s.bil'
                                         % (filename, keyS))
                    fileh = os.path.join(outfolder, '%s_%s.hdr'
                                         % (filename, keyS))
                    fileg = os.path.join(outfolder, '%s_%s.tif'
                                         % (filename, keyS))

                    GDALGrid.copyFromGrid(maplayers[key]['grid']).save(filen)
                    cmd = 'gdal_translate -a_srs EPSG:4326 -of GTiff %s %s' % (
                        filen, fileg)
                    rc, so, se = get_command_output(cmd)
                    # Delete bil file and its header
                    os.remove(filen)
                    os.remove(fileh)
                    filenames.append(fileg)

            if args.make_webpage:
                # Compile into list of results for later
                results.append(maplayers)

                # Make binary output for ShakeCast
                filef = os.path.join(outfolder, '%s_model.flt'
                                     % filename)
                # And get name of header
                filefh = os.path.join(outfolder, '%s_model.hdr'
                                      % filename)
                # Make file
                write_floats(filef, maplayers['model']['grid'])
                filenames.append(filef)
                filenames.append(filefh)

            if args.make_summary and not args.make_webpage:
                # Compile into list of results for later
                results.append(maplayers)

        if args.make_webpage:
            outputs = hazdev(
                results, configs,
                shakefile, outfolder=outfolder,
                pop_file=args.popfile,
                pager_alert=args.property_alertlevel)
            filenames = filenames + outputs

        if args.make_summary:
            outputs = GFSummary(
                results, configs, args.web_template,
                shakefile, outfolder=outfolder, cleanup=True,
                faultfile=ffault, point=point, pop_file=args.popfile)
            filenames = filenames + outputs

#        # create transparent png file
#        outputs = create_png(outdir)
#        filenames = filenames + outputs
#
#        # create info file
#        infofile = create_info(outdir)
#        filenames = filenames + infofile

        print('\nFiles created:\n')
        for filen in filenames:
            print('%s' % filen)

        return filenames


def getGridURL(gridurl):
    """
    Args:
        gridurl (str): url for Shakemap grid.xml file.

    Returns:
        file object corresponding to the url.
    """

    f = None
    fh = None
    with urllib.request.urlopen(gridurl) as fh:
        data = fh.read().decode('utf-8')
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write(data)

    return f.name


def isURL(gridurl):
    """
    This function determines if the provided string is a valid url

    Args:
        gridurl (str): url to check.

    Returns:
        bool: True if gridurl is a valid url, False otherwise.
    """

    isURL = False
    try:
        urllib.request.urlopen(gridurl)
        isURL = True
    except:
        pass
    return isURL


def set_default_paths(args):
    """
    Creates a file called .gfail_defaults that contains default path
    information to simplify running gfail. Can be overwritten by any manually
    entered paths. This updates any existing .gfail_defaults file. If
    args.data_path is 'reset' then any existing defaults will be removed.

    Args:
        args (arparser Namespace): Input arguments.

    Returns:
        Updates .gfail_defaults file on users path, or creates new one if
        file does not already exist.
    """
    filename = os.path.join(os.path.expanduser('~'), '.gfail_defaults')
    if os.path.exists(filename):
        D = ConfigObj(filename)
    else:
        D = {}
    if args.data_path is not None:
        if args.data_path == 'reset':
            D.pop('data_path')
        else:
            # check that it's a valid path
            if os.path.exists(args.data_path):
                D.update({'data_path': args.data_path})
            else:
                print('Path given for data_path does not exist: %s'
                      % args.data_path)
    if args.output_filepath is not None:
        if args.output_filepath == 'reset':
            D.pop('output_filepath')
        else:
            # check that it's a valid path
            if os.path.exists(args.output_filepath):
                D.update({'output_filepath': args.output_filepath})
            else:
                print('Path given for output_filepath does not exist: %s'
                      % args.output_filepath)
    if args.config_filepath is not None:
        if args.config_filepath == 'reset':
            D.pop('config_filepath')
        else:
            # check that it's a valid path
            if os.path.exists(args.config_filepath):
                D.update({'config_filepath': args.config_filepath})
            else:
                print('Path given for config_filepath does not exist: %s'
                      % args.config_filepath)
    if args.mapconfig is not None:
        if args.mapconfig == 'reset':
            D.pop('mapconfig')
        else:
            # check that it's a valid path
            if os.path.exists(args.mapconfig):
                D.update({'mapconfig': args.mapconfig})
            else:
                print('Path given for mapconfig does not exist: %s'
                      % args.mapconfig)
    if args.mapdata_filepath is not None:
        if args.mapdata_filepath == 'reset':
            D.pop('mapdata_filepath')
        else:
            # check that it's a valid path
            if os.path.exists(args.mapdata_filepath):
                D.update({'mapdata_filepath': args.mapdata_filepath})
            else:
                print('Path given for mapdata_filepath does not exist: %s'
                      % args.mapdata_filepath)
    if args.popfile is not None:
        if args.popfile == 'reset':
            D.pop('popfile')
        else:
            # check that it's a valid path
            if os.path.exists(args.popfile):
                D.update({'popfile': args.popfile})
            else:
                print('Path given for population file does not exist: %s'
                      % args.popfile)
    if args.web_template is not None:
        if args.web_template == 'reset':
            D.pop('web_template')
        else:
            # check that it's a valid path
            if os.path.exists(args.web_template):
                D.update({'web_template': args.web_template})
            else:
                print('Path given for webpage templates does not exist: %s'
                      % args.web_template)
    if args.trimfile is not None:
        if args.trimfile == 'reset':
            D.pop('trim')
        else:
            # check that it's a valid path and that it's a shapefile
            if os.path.exists(args.trimfile):
                filename4, fileextension = os.path.splitext(args.trimfile)
                if fileextension == '.shp':
                    D.update({'trimfile': args.trimfile})
                else:
                    print('Ocean trimming file is not a shapefile: %s'
                          % args.trimfile)
            else:
                print('Path given for ocean trimming file does not exist: %s'
                      % args.trimfile)
    if args.pdl_config is not None:
        if args.pdl_config == 'reset':
            D.pop('pdl_config')
        else:
            # check that it's a valid path
            if os.path.exists(args.pdl_config):
                D.update({'pdl_config': args.pdl_config})
            else:
                print('Path given for pdl config file does not exist: %s'
                      % args.pdl_config)
    if args.log_filepath is not None:
        if args.log_filepath == 'reset':
            D.pop('log_filepath')
        else:
            # check that it's a valid path
            if os.path.exists(args.log_filepath):
                D.update({'log_filepath': args.log_filepath})
            else:
                print('Path given for log file does not exist: %s'
                      % args.log_filepath)
    if args.dbfile is not None:
        if args.dbfile == 'reset':
            D.pop('dbfile')
        else:
            # check that it's a valid path
            if os.path.exists(args.dbfile):
                D.update({'dbfile': args.dbfile})
            else:
                print('Path given for database file does not exist: %s'
                      % args.dbfile)

    print('New default paths set.\n')

    if D:
        C = ConfigObj(D)
        C.filename = filename
        C.write()
        list_default_paths()
    else:
        print('no defaults set because no paths were input\n')


def list_default_paths():
    """
    Lists all default paths currently set.
    """
    filename = os.path.join(os.path.expanduser('~'), '.gfail_defaults')
    if os.path.exists(filename):
        D = ConfigObj(filename)
        print('Default paths currently set to:\n')
        for key in D:
            print('\t%s = %s' % (key, D[key]))
    else:
        print('No default paths currently set\n')


def reset_default_paths():
    """
    Clear default path file
    """
    filename = os.path.join(os.path.expanduser('~'), '.gfail_defaults')
    if os.path.exists(filename):
        os.remove(filename)
        print('Default paths cleared\n')
    else:
        print('No default paths currently set\n')


def get_bounds(shakefile, parameter='pga', threshold=2.0):
    """
    Get the boundaries of the shakemap that include all areas with shaking
    above the defined threshold.

    Args:
        shakefile (str): Path to shakemap file.
        parameter (str): Either 'pga' or 'pgv'.
        threshold (float): Minimum value of parameter of interest, in units
            of %g for pga and cm/s for pgv. The default value of 2% g is based
            on minimum pga threshold ever observed to have triggered landslides
            by Jibson and Harp (2016).

    Returns:
        dict: A dictionary with keys 'xmin', 'xmax', 'ymin', and 'ymax' that
        defines the boundaries in geographic coordinates.
    """
    shakemap = ShakeGrid.load(shakefile, adjust='res')
    if parameter == 'pga':
        vals = shakemap.getLayer('pga')
    elif parameter == 'pgv':
        vals = shakemap.getLayer('pgv')
    else:
        raise Exception('parameter not valid')
    xmin, xmax, ymin, ymax = vals.getBounds()
    lons = np.linspace(xmin, xmax, vals.getGeoDict().nx)
    lats = np.linspace(ymax, ymin, vals.getGeoDict().ny)
    row, col = np.where(vals.getData() > float(threshold))
    lonmin = lons[col].min()
    lonmax = lons[col].max()
    latmin = lats[row].min()
    latmax = lats[row].max()

    # dummy fillers, only really care about bounds
    boundaries1 = {'dx': 100, 'dy': 100., 'nx': 100., 'ny': 100}

    if xmin < lonmin:
        boundaries1['xmin'] = lonmin
    else:
        boundaries1['xmin'] = xmin
    if xmax > lonmax:
        boundaries1['xmax'] = lonmax
    else:
        boundaries1['xmax'] = xmax
    if ymin < latmin:
        boundaries1['ymin'] = latmin
    else:
        boundaries1['ymin'] = ymin
    if ymax > latmax:
        boundaries1['ymax'] = latmax
    else:
        boundaries1['ymax'] = ymax

    return boundaries1
