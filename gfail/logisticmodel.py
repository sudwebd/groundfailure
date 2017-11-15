#!/usr/bin/env python
"""
This module contains functions and class definitions for running forward models of models based on logistic regression.
"""

#stdlib imports
import numpy as np
import os.path
import re
import collections
import copy
import subprocess
#from scipy import sparse
import shutil
import tempfile
from timeit import default_timer as timer

#third party imports
from mapio.shake import ShakeGrid
from mapio.shake import getHeaderData
from mapio.gmt import GMTGrid
from mapio.gdal import GDALGrid
from mapio.grid2d import Grid2D
from mapio.geodict import GeoDict
#from osgeo import gdal

from gfail.temphdf import TempHdf

PARAM_PATTERN = 'b[0-9]+'
LAYER_PATTERN = '_layer'
TERM_PATTERN = 'term'

SM_TERMS = ['MW', 'YEAR', 'MONTH', 'DAY', 'HOUR', 'pga', 'pgv', 'mmi']
SM_GRID_TERMS = ['pga', 'pgv', 'mmi']
OPERATORS = ['log', 'log10', 'arctan', 'power', 'sqrt', 'minimum', 'pi']  # these will get np. prepended
FLOATPAT = '[+-]?(?=\d*[.eE])(?=\.?\d)\d*\.?\d*(?:[eE][+-]?\d+)?'
INTPAT = '[0-9]+'
OPERATORPAT = '[\+\-\*\/]*'
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct',
          'Nov', 'Dec']


class LogisticModel(object):
    def __init__(self, shakefile, config, uncertfile=None, saveinputs=False,
                 slopefile=None, bounds=None, numstd=1, slopemod=None):
        """
        Set up the logistic model
        Args:
            shakefile (str): Path to shakemap grid.xml file for the event.
            config: configobj object defining the model and its inputs. Only
                one model should be described in each config file.
            uncertfile (str): Path to uncertainty.xml file.
            saveinputs (bool): Save input layers as Grid2D objects in addition
                to the model? If false (the default), it will just output the
                model.
            slopefile (str): Optional path to slopefile that will be resampled
                to the other input files for applying thresholds. OVERWRITES
                VALUE IN CONFIG.
            bounds (dict): Default of None uses ShakeMap boundaries, otherwise
                a dictionary of boundaries to cut to like
                .. code-block:: python
                    bounds = {
                        'xmin': lonmin, 'xmax': lonmax,
                        'ymin': latmin, 'ymax': latmax
                    }
            numstd (float): Number of +/- standard deviations to use if
                uncertainty is computed.
            slopemod (str): How slope input should be modified to be in
                degrees: e.g., ``np.arctan(slope) * 180. / np.pi`` or
                ``slope/100.`` (note that this may be in the config file
                already).
        """
        mnames = getLogisticModelNames(config)
        if len(mnames) == 0:
            raise Exception('No config file found or problem with config '
                            'file format')
        if len(mnames) > 1:
            raise Exception('Config file contains more than one model which '
                            'is no longer allowed, update your config file '
                            'to the newer format')
        self.model = mnames[0]
        self.config = config
        cmodel = config[self.model]
        self.modeltype = cmodel['gfetype']
        self.coeffs = validateCoefficients(cmodel)
        # key = layer name, value = file name
        self.layers = validateLayers(cmodel)
        self.terms, timeField = validateTerms(cmodel, self.coeffs, self.layers)
        self.interpolations = validateInterpolations(cmodel, self.layers)
        self.units = validateUnits(cmodel, self.layers)
        self.gmused = [value for term, value in cmodel['terms'].items()
                       if 'pga' in value.lower() or 'pgv' in
                       value.lower() or 'mmi' in value.lower()]
        self.modelrefs, self.longrefs, self.shortrefs = validateRefs(cmodel)
        self.numstd = numstd
        if cmodel['baselayer'] not in list(self.layers.keys()):
            raise Exception('You must specify a base layer corresponding to '
                            'one of the files in the layer section.')
        self.saveinputs = saveinputs
        if slopefile is None:
            try:
                self.slopefile = cmodel['slopefile']
            except:
                print('Could not find slopefile term in config, no slope '
                      'thresholds will be applied\n')
                self.slopefile = None
        else:
            self.slopefile = slopefile
        if slopemod is None:
            try:
                self.slopemod = cmodel['slopemod']
            except:
                self.slopemod = None

        # Get month of event
        griddict, eventdict, specdict, fields, uncertainties = \
            getHeaderData(shakefile)
        MONTH = MONTHS[(eventdict['event_timestamp'].month) - 1]

        # Figure out how/if need to cut anything
        geodict = ShakeGrid.getFileGeoDict(shakefile, adjust='res')
        if bounds is not None:  # Make sure bounds are within ShakeMap Grid
            if (geodict.xmin > bounds['xmin'] or
                    geodict.xmax < bounds['xmax'] or
                    geodict.ymin > bounds['ymin'] or
                    geodict.ymax < bounds['ymax']):
                print('Specified bounds are outside shakemap area, using '
                      'ShakeMap bounds instead.')
                bounds = None
        if bounds is not None:
            tempgdict = GeoDict.createDictFromBox(
                bounds['xmin'], bounds['xmax'],
                bounds['ymin'], bounds['ymax'],
                geodict.dx, geodict.dy, inside=False)
            gdict = geodict.getBoundsWithin(tempgdict)
        else:
            gdict = geodict

        # Now find the layer that is our base layer and get the largest bounds
        # we can guarantee not to exceed shakemap bounds
        basefile = self.layers[cmodel['baselayer']]
        ftype = getFileType(basefile)
        if ftype == 'esri':
            basegeodict, firstcol = GDALGrid.getFileGeoDict(basefile)
            sampledict = basegeodict.getBoundsWithin(gdict)
        elif ftype == 'gmt':
            basegeodict, firstcol = GMTGrid.getFileGeoDict(basefile)
            sampledict = basegeodict.getBoundsWithin(gdict)
        else:
            raise Exception('All predictor variable grids must be a valid '
                            'GMT or ESRI file type.')

        # Find slope thresholds, if applicable
        try:
            self.slopemin = float(config[self.model]['slopemin'])
            self.slopemax = float(config[self.model]['slopemax'])
        except:
            print('Could not find slopemin and/or slopemax in config, limits '
                  'of 0 to 90 degrees will be used.')
            self.slopemin = 0.
            self.slopemax = 90.

        # Make temporary directory for hdf5 pytables file storage
        self.tempdir = tempfile.mkdtemp()
        # Apply to shakemap
        # now load the shakemap, resampling and padding if necessary
        start = timer()
        temp = ShakeGrid.load(shakefile, samplegeodict=sampledict,
                              resample=True, doPadding=True,
                              adjust='res')
        self.shakemap = TempHdf(temp, os.path.join(self.tempdir,
                                                   'shakemap.hdf5'))
        del(temp)
        print('Shakemap loading: %1.1f sec' % (timer() - start))

        # take uncertainties into account
        if uncertfile is not None:
            try:
                temp = ShakeGrid.load(uncertfile, samplegeodict=sampledict,
                                      resample=True, doPadding=True,
                                      adjust='res')
                self.uncert = TempHdf(temp, os.path.join(self.tempdir,
                                                         'uncert.hdf5'))
                del(temp)
            except:
                print('Could not read uncertainty file, ignoring '
                      'uncertainties')
                self.uncert = None
        else:
            self.uncert = None

        # Load the predictor layers, save as hdf5 temporary files, put file
        # locations into a dictionary.

        # Will be replaced in the next section if a slopefile was defined
        self.nonzero = None

        # key = layer name, value = grid object
        self.layerdict = {}

        didslope = False
        for layername, layerfile in self.layers.items():
            start = timer()
            if isinstance(layerfile, list):
                for lfile in layerfile:
                    if timeField == 'MONTH':
                        if lfile.find(MONTH) > -1:
                            layerfile = lfile
                            ftype = getFileType(layerfile)
                            interp = self.interpolations[layername]
                            if ftype == 'gmt':
                                temp = GMTGrid.load(layerfile,
                                                    sampledict,
                                                    resample=True,
                                                    method=interp,
                                                    doPadding=True)
                            elif ftype == 'esri':
                                temp = GDALGrid.load(layerfile,
                                                     sampledict,
                                                     resample=True,
                                                     method=interp,
                                                     doPadding=True)
                            else:
                                msg = ('Layer %s (file %s) does not appear '
                                       'to be a valid GMT or ESRI file.'
                                       % (layername, layerfile))
                                raise Exception(msg)
                            self.layerdict[layername] = TempHdf(
                                temp, os.path.join(self.tempdir,
                                                   '%s.hdf5' % layername))
                            del(temp)
            else:
                interp = self.interpolations[layername]

                # If resolution is too high, first create temporary geotiff
                # snippet using gdal because mapio can't handle cutting high
                # res files
                templyrname = os.path.join(self.tempdir, '%s.tif' % layername)

                # cut piece out quickly using gdal
                newgdict = quickcut(layerfile, templyrname, sampledict,
                                    extrasamp=5., method='nearest')

                # Then load it in using mapio
                if newgdict.isAligned(sampledict):
                    temp = GDALGrid.load(templyrname,
                                         sampledict,
                                         resample=False)
                else:
                    temp = GDALGrid.load(templyrname,
                                         sampledict,
                                         resample=True,
                                         method=interp,
                                         doPadding=True)

                self.layerdict[layername] = TempHdf(
                    temp, os.path.join(self.tempdir, '%s.hdf5' % layername))

                if layerfile == self.slopefile:
                    flag = 0
                    if self.slopemod is None:
                        slope1 = temp.getData().astype(float)
                        slope = 0
                    else:
                        try:
                            slope = temp.getData().astype(float)
                            slope1 = eval(self.slopemod)
                        except:
                            print('slopemod provided not valid, continuing '
                                  'without slope thresholds.')
                            flag = 1
                    if flag == 0:
                        nonzero = np.array(
                            [(slope1 > self.slopemin) &
                             (slope1 <= self.slopemax)])
                        self.nonzero = nonzero[0, :, :]
                        del(slope1)
                        del(slope)
                    didslope = True
                del(temp)

            print('Loading of layer %s: %1.1f sec'
                  % (layername, timer() - start))

        if didslope is False and self.slopefile is not None:
            # Slope didn't get read in yet

            # If resolution is too high, first create temporary geotiff snippet
            # using gdal because mapio can't handle cutting high res files.
            templyrname = os.path.join(self.tempdir, 'tempslope.tif')

            # Cut piece out quickly using gdal
            newgdict = quickcut(self.slopefile, templyrname, sampledict,
                                extrasamp=5., method='nearest')

            # Then load it in using mapio
            if newgdict.isAligned(sampledict):
                temp = GDALGrid.load(templyrname, sampledict, resample=False)
            else:
                temp = GDALGrid.load(templyrname, sampledict, resample=True,
                                     method=interp, doPadding=True)
            flag = 0
            if self.slopemod is None:
                slope1 = temp.getData().astype(float)
                slope = 0
            else:
                try:
                    slope = temp.getData().astype(float)
                    slope1 = eval(self.slopemod)
                except:
                    print('slopemod provided not valid, continuing without '
                          'slope thresholds')
                    flag = 1
            if flag == 0:
                nonzero = np.array([(slope1 > self.slopemin) &
                                    (slope1 <= self.slopemax)])
                self.nonzero = nonzero[0, :, :]
                del(slope1)
                del(slope)

        self.nuggets = [str(self.coeffs['b0'])]

        ckeys = list(self.terms.keys())
        ckeys.sort()
        for key in ckeys:
            term = self.terms[key]
            coeff = self.coeffs[key]
            self.nuggets.append('(%g * %s)' % (coeff, term))

        self.equation = ' + '.join(self.nuggets)

        if self.uncert is not None:
            self.nugmin = copy.copy(self.nuggets)
            self.nugmax = copy.copy(self.nuggets)

            # Find the term with the shakemap input and replace for these
            # nuggets.
            for k, nug in enumerate(self.nuggets):
                if ("self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pga')" in nug):
                    self.nugmin[k] = self.nugmin[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pga')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='pga')) - "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdpga')))")
                    self.nugmax[k] = self.nugmax[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pga')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='pga')) + "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdpga')))")
                elif ("self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pgv')" in nug):
                    self.nugmin[k] = self.nugmin[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pgv')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='pgv')) - "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdpgv')))")
                    self.nugmax[k] = self.nugmax[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='pgv')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='pgv')) + "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdpgv')))")
                elif ("self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='mmi')" in nug):
                    self.nugmin[k] = self.nugmin[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='mmi')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='mmi')) - "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdmmi')))")
                    self.nugmax[k] = self.nugmax[k].replace(
                        "self.shakemap.getSlice(rowstart, rowend, colstart, "
                        "colend, name='mmi')",
                        "(np.exp(np.log(self.shakemap.getSlice(rowstart, "
                        "rowend, colstart, colend, name='mmi')) + "
                        "self.numstd * self.uncert.getSlice(rowstart, "
                        "rowend, colstart, colend, name='stdmmi')))")
            self.equationmin = ' + '.join(self.nugmin)
            self.equationmax = ' + '.join(self.nugmax)
        else:
            self.equationmin = None
            self.equationmax = None

        self.geodict = self.shakemap.getGeoDict()

    def getEquations(self):
        """
        Method for LogisticModel class to extract strings defining the
        equations for the model for median ground motions and +/- one standard
        deviation (3 total).
        Returns:
            tuple: Three equations: equation, equationmin, equationmax, where
                * equation is the equation for median ground motions,
                * equationmin is the equation for the same model but with
                  median ground motions minus 1 standard deviation, and
                * equationmax is the same but for plus 1 standard deviation.
        """
        return self.equation, self.equationmin, self.equationmax

    def getGeoDict(self):
        """
        Returns the geodictionary of the LogisticModel class defining bounds
        and resolution of model inputs and outputs.
        Returns:
            geodict: mapio geodict object.
        """
        return self.geodict

    def calculate(self, cleanup=True, rowmax=300, colmax=None):
        """
        Calculate the model.
        Args:
            cleanup (bool): Delete temporary hdf5 files?
            rowmax (int): Number of rows to compute at once; None does all at
                once.
            colmax (int): Number of columns to compute at once; None does all
                at once.
        Returns:
            dict: Dictionary containing the model results (and model inputs if
            saveinputs was set to True). See
            `the description <https://github.com/usgs/groundfailure#api-for-model-output>`_
            of the structure.
        """

        # Figure out what slices to do
        rowstarts, rowends, colstarts, colends = \
            self.shakemap.getSliceDiv(rowmax, colmax)

        # Make empty matrix to fill
        X = np.empty([self.geodict.ny, self.geodict.nx])

        # Loop through slices, appending output each time
        for rowstart, rowend, colstart, colend in \
                zip(rowstarts, rowends, colstarts, colends):
            X[rowstart:rowend, colstart:colend] = eval(self.equation)

        P = 1/(1 + np.exp(-X))

        if 'vs30max' in self.config[self.model].keys():
            vs30 = self.layerdict['vs30'].getSlice(
                None, None, None, None, name='vs30')
            P[vs30 > float(self.config[self.model]['vs30max'])] = 0.0

        if 'minpgv' in self.config[self.model].keys():
            pgv = self.shakemap.getSlice(None, None, None, None, name='pgv')
            P[pgv < float(self.config[self.model]['minpgv'])] = 0.0

        if 'coverage' in self.config[self.model].keys():
            eqn = self.config[self.model]['coverage']['eqn']
            P = eval(eqn)

        if self.uncert is not None:
            # Make empty matrix to fill
            Xmin = np.empty([self.geodict.ny, self.geodict.nx])
            Xmax = Xmin.copy()
            # Loop through slices, appending output each time
            for rowstart, rowend, colstart, colend in \
                    zip(rowstarts, rowends, colstarts, colends):
                Xmin[rowstart:rowend, colstart:colend] = eval(self.equationmin)
                Xmax[rowstart:rowend, colstart:colend] = eval(self.equationmax)

            Pmin = 1/(1 + np.exp(-Xmin))
            Pmax = 1/(1 + np.exp(-Xmax))

            if 'vs30max' in self.config[self.model].keys():
                vs30 = self.layerdict['vs30'].getSlice(
                    None, None, None, None, name='vs30')
                Pmin[vs30 > float(self.config[self.model]['vs30max'])] = 0.0
                Pmax[vs30 > float(self.config[self.model]['vs30max'])] = 0.0

            if 'minpgv' in self.config[self.model].keys():
                pgv = self.shakemap.getSlice(
                    None, None, None, None, name='pgv')
                Pmin[pgv < float(self.config[self.model]['minpgv'])] = 0.0
                Pmax[pgv < float(self.config[self.model]['minpgv'])] = 0.0

            if 'coverage' in self.config[self.model].keys():
                eqnmin = eqn.replace('P', 'Pmin')
                eqnmax = eqn.replace('P', 'Pmax')
                Pmin = eval(eqnmin)
                Pmax = eval(eqnmax)
        if self.slopefile is not None and self.nonzero is not None:
            # Apply slope min/max limits
            print('applying slope thresholds')
            P = P * self.nonzero
            P[np.isnan(P)] = 0.0
            if self.uncert is not None:
                Pmin = Pmin * self.nonzero
                Pmax = Pmax * self.nonzero
                Pmin[np.isnan(Pmin)] = 0.0
                Pmax[np.isnan(Pmax)] = 0.0
        else:
            print('No slope file provided, slope thresholds not applied')

        # Stuff into Grid2D object
        if 'Jessee' in self.modelrefs['shortref']:
            if 'coverage' not in self.config[self.model].keys():
                units5 = 'relative hazard'
            else:
                units5 = 'areal coverage'
        else:
            units5 = 'probability'

        temp = self.shakemap.getShakeDict()
        shakedetail = (
            '%s_ver%s'
            % (temp['shakemap_id'],
               temp['shakemap_version']))
        description = {
            'name': self.modelrefs['shortref'],
            'longref': self.modelrefs['longref'],
            'units': units5,
            'shakemap': shakedetail,
            'parameters': {'slopemin': self.slopemin,
                           'slopemax': self.slopemax,
                           'modeltype': self.modeltype}}
        Pgrid = Grid2D(P, self.geodict)
        rdict = collections.OrderedDict()
        rdict['model'] = {
            'grid': Pgrid,
            'label': ('%s %s') % (self.modeltype.capitalize(),
                                  units5.title()),
            'type': 'output',
            'description': description
        }
        if self.uncert is not None:
            rdict['modelmin'] = {
                'grid': Grid2D(Pmin, self.geodict),
                'label': ('%s %s (-%0.1f std ground motion)'
                          % (self.modeltype.capitalize(),
                             units5.title(),
                             self.numstd)),
                'type': 'output',
                'description': description
            }
            rdict['modelmax'] = {
                'grid': Grid2D(Pmax, self.geodict),
                'label': ('%s %s (+%0.1f std ground motion)'
                          % (self.modeltype.capitalize(),
                             units5.title(),
                             self.numstd)),
                'type': 'output',
                'description': description
            }

        # This step might swamp memory for higher resolution runs
        if self.saveinputs is True:
            for layername, layergrid in list(self.layerdict.items()):
                units = self.units[layername]
                if units is None:
                    units = ''
                rdict[layername] = {
                    'grid': Grid2D(
                        layergrid.getSlice(
                            None, None, None, None, name=layername),
                        self.geodict
                    ),
                    'label': '%s (%s)' % (layername, units),
                    'type': 'input',
                    'description': {
                        'units': units,
                        'shakemap': shakedetail
                    }
                }
            for gmused in self.gmused:
                if 'pga' in gmused:
                    units = '%g'
                    getkey = 'pga'
                elif 'pgv' in gmused:
                    units = 'cm/s'
                    getkey = 'pgv'
                elif 'mmi' in gmused:
                    units = 'intensity'
                    getkey = 'mmi'
                else:
                    continue
                    # Layer is derived from several input layers, skip
                    # outputting this layer

                if getkey in rdict:
                    continue

                layer = self.shakemap.getSlice(
                    None, None, None, None, name=getkey)
                rdict[getkey] = {
                    'grid': Grid2D(layer, self.geodict),
                    'label': '%s (%s)' % (getkey.upper(), units),
                    'type': 'input',
                    'description': {
                        'units': units,
                        'shakemap': shakedetail
                    }
                }
                if self.uncert is not None:
                    uncertlayer = self.uncert.getSlice(
                        None, None, None, None, name='std'+getkey)
                    layer1 = np.exp(np.log(layer) - uncertlayer)
                    rdict[getkey + 'modelmin'] = {
                        'grid': Grid2D(layer1, self.geodict),
                        'label': ('%s - %0.1f std (%s)'
                                  % (getkey.upper(),
                                     self.numstd, units)),
                        'type': 'input',
                        'description': {'units': units,
                                        'shakemap': shakedetail}
                    }
                    layer2 = np.exp(np.log(layer) + uncertlayer)
                    rdict[getkey + 'modelmax'] = {
                        'grid': Grid2D(layer2, self.geodict),
                        'label': ('%s + %0.1f std (%s)'
                                  % (getkey.upper(),
                                     self.numstd, units)),
                        'type': 'input',
                        'description': {'units': units,
                                        'shakemap': shakedetail}
                    }
        if cleanup:
            shutil.rmtree(self.tempdir)
        return rdict


def getLogisticModelNames(config):
    """
    Get the names of the models present in the configobj
    Args:
        config: configobj object defining the model and its inputs.
    Returns:
        list: list of model names.
    """
    names = []
    lmodel_space = config
    for key, value in lmodel_space.items():
        if isinstance(value, str):
            continue
        else:  # this is a model
            names.append(key)
    return names


def getFileType(filename):
    """
    Determine whether input file is a shapefile or a grid (ESRI or GMT).
    EVENTUALLY WILL BE MOVED TO MAPIO.
    Args:
        filename (str): Path to candidate filename.
    Returns:
        str: 'shapefile', 'grid', or 'unknown'.
    """
    if os.path.isdir(filename):
        return 'dir'
    ftype = GMTGrid.getFileType(filename)
    if ftype != 'unknown':
        return 'gmt'
    # Skip over ESRI header files
    if filename.endswith('.hdr'):
        return 'unknown'
    try:
        GDALGrid.getFileGeoDict(filename)
        return 'esri'
    except:
        pass
    return 'unknown'


def getAllGridFiles(indir):
    """
    Get list of all gmt or esri (.grd, .bil) files in a directory.
    EVENTUALLY WILL BE MOVED TO MAPIO
    Args:
        indir (str): Directory to search.
    Returns:
        list: List of file names.
    """
    tflist = os.listdir(indir)
    flist = []
    for tf in tflist:
        fullfile = os.path.join(indir, tf)
        ftype = getFileType(fullfile)
        if ftype in ['gmt', 'esri']:
            flist.append(fullfile)
    return flist


def validateCoefficients(cmodel):
    """
    Ensures coefficients provided in model description are valid and outputs
    a dictionary of the coefficients.
    Args:
        cmodel (dict): Sub-dictionary from config for specific model,
            e.g.
            .. code-block:: python
                cmodel = config['test_model']
    Returns:
        dict: a dictionary of model coefficients named b0, b1, b2...
    """
    coeffs = {}
    for key, value in cmodel['coefficients'].items():
        if re.search('b[0-9]*', key) is None:
            raise Exception('coefficients must be named b0, b1, ...')
        coeffs[key] = float(value)
    if 'b0' not in list(coeffs.keys()):
        raise Exception('coefficients must include an intercept '
                        'coefficient named b0.')
    return coeffs


def validateLayers(cmodel):
    """
    Ensures all input files required to run the model exist and are valid
    file types.
    Args:
        cmodel (dict): Sub-dictionary from config for specific model,
            e.g.
            .. code-block:: python
                cmodel = config['test_model']
    Returns:
        dict: a dictionary of file names, e.g.
        .. code-block:: python
            {
                'slope': 'slopefile.bil',
                'vs30': 'vs30.grd'
            }
    """
    layers = {}
    for key in cmodel['layers'].keys():
        for item, value in cmodel['layers'][key].items():
            if item == 'file':
                ftype = getFileType(value)
                if ftype == 'unknown':
                    raise Exception('layer file %s is not a valid GMT or '
                                    'ESRI file.' % value)
                if ftype == 'dir':
                    value = getAllGridFiles(value)
                layers[key] = value
    return layers


def validateTerms(cmodel, coeffs, layers):
    """
    Reformats model inputs from config file, replacing functions with numpy
    functions, inserting code for extracting data from each layer (required
    to run eval in the calculate step), addressing any time variables, and
    checks that term names match coefficient names.
    TODO: return a time field for every term, not just one global one.
    Args:
        cmodel (dict): Sub-dictionary from config for specific model,
            e.g.
            .. code-block:: python
                cmodel = config['test_model']
        coeffs (dict): Dictionary of model coefficients, e.g.
            .. code-block:: python
                {'b0': 3.5, 'b1': -0.01}
        layers (dict): Dictionary of file names for all input layers, e.g.
            .. code-block:: python
                {'slope': 'slopefile.bil', 'vs30': 'vs30.grd'}
    Returns:
        tuple: (terms, timeField), where
            - 'terms' is a dictionary of terms that form the model equation,
              e.g.
            .. code-block:: python
                {
                    'b1': "self.layerdict['friction'].getData()",
                    'b2': "self.layerdict['slope'].getData()/100."
                }
            - 'timeField' indicates the time that is used to know which input
              file to read in, e.g. for monthly average precipitation, 'MONTH'.
    """
    terms = {}
    timeField = None
    for key, value in cmodel['terms'].items():
        if key not in list(coeffs.keys()):
            raise Exception('Term names must match names of coefficients')
        # replace log with np.log, make sure variables are all in layers list,
        # etc.
        term, rem, tTimeField = checkTerm(value, layers)
        if tTimeField is not None:
            timeField = tTimeField
        if len(rem):
            msg = ('Term "%s" contains the unknown text fragment "%s". '
                   'This may cause the expression to fail.')
            tpl = (term, rem)
            raise Exception(msg % tpl)
        terms[key] = term
    return (terms, timeField)


def quickcut(filename, tempname, gdict, extrasamp=5, method='nearest'):
    """
    Use gdal to trim a large global file down quickly so mapio can read it
    efficiently.
    Using subprocess approach because ``gdal.Translate`` doesn't hang on the
    command until the file is created which causes problems in the next steps.
    Args:
        filename (str): File path to original input file (raster).
        tempname (str): File path to desired location of clipped part of
            filename.
        gdict (geodict): Geodictionary to cut around and align with.
        extrasamp (int): Number of extra cells to cut around each edge of
            geodict to have resampling buffer for future steps.
        method (str): If resampling is necessary, method to use.
    Returns:
        dict: Geodict for new file that was cut and also creates the file in
        the tempname location.
    """
    try:
        filegdict = ShakeGrid.getFileGeoDict(filename, adjust='res')
    except:
        try:
            filegdict = GDALGrid.getFileGeoDict(filename)
        except:
            try:
                filegdict = GMTGrid.getFileGeoDict(filename)
            except:
                raise Exception('Cannot get geodict for %s' % filename)

    filegdict = filegdict[0]
    tempgdict = GeoDict.createDictFromBox(
        gdict.xmin, gdict.xmax, gdict.ymin, gdict.ymax,
        filegdict.dx, filegdict.dy, inside=True)
    egdict = filegdict.getBoundsWithin(tempgdict)

    ulx = egdict.xmin - extrasamp * egdict.dx
    uly = egdict.ymax + extrasamp * egdict.dy
    lrx = egdict.xmax + extrasamp * egdict.dx
    lry = egdict.ymin - extrasamp * egdict.dy

    with open(os.devnull, 'w') as devnull:
        subprocess.call(
            'gdal_translate -of GTiff -projwin %1.8f %1.8f %1.8f %1.8f '
            '-r %s %s %s' % (ulx, uly, lrx, lry, method, filename, tempname),
            shell=True, stdout=devnull)
    newgdict = GDALGrid.getFileGeoDict(tempname)[0]
    return newgdict
    # TODO add error catching for subprocess call


def validateInterpolations(cmodel, layers):
    interpolations = {}
    for key, value in cmodel['interpolations'].items():
        if key not in list(layers.keys()):
            raise Exception(
                'Interpolation key %s does not match any names of layers'
                % key)
        methods = ['linear', 'nearest', 'cubic']
        if value not in methods:
            raise Exception(
                'Interpolation method %s not in approved list of methods: %s'
                % (key, str(methods)))
        interpolations[key] = value
    for key in list(layers.keys()):
        if key not in list(interpolations.keys()):
            raise Exception(
                'No interpolation method configured for layer %s' % key)
    return interpolations


def validateUnits(cmodel, layers):
    units = {}
    for key in cmodel['layers'].keys():
        if 'units' in cmodel['layers'][key]:
            units[key] = cmodel['layers'][key]['units']
        else:
            raise Exception('No unit string configured for layer %s' % key)
    return units


def validateLogisticModels(config):
    mnames = getLogisticModelNames(config)
    if len(mnames) > 1:
        raise Exception('Config file contains more than one model which is '
                        'no longer allowed, update your config file to the '
                        'newer format')
    for cmodelname in mnames:
        try:
            cmodel = config[cmodelname]
            coeffs = validateCoefficients(cmodel)
            # key = layer name, value = file name
            layers = validateLayers(cmodel)
            terms, timeField = validateTerms(cmodel, coeffs, layers)
            if timeField is not None:
                for (layer, layerfile) in list(layers.items()):
                    if isinstance(layerfile, list):
                        for lfile in layerfile:
                            if timeField == 'MONTH':
                                pass
            validateInterpolations(cmodel, layers)
            if cmodel['baselayer'] not in layers:
                raise Exception(
                    'Model %s missing baselayer parameter.' % cmodelname)
        except Exception as e:
            raise Exception('Validation failed with error: "%s" on model %s'
                            % (str(e), cmodelname))

    return True


def validateRefs(cmodel):
    longrefs = {}
    shortrefs = {}
    modelrefs = {}
    for key in cmodel['layers'].keys():
        if 'longref' in cmodel['layers'][key]:
            longrefs[key] = cmodel['layers'][key]['longref']
        else:
            print('No longref provided for layer %s' % key)
            longrefs[key] = 'unknown'
        if 'shortref' in cmodel['layers'][key]:
            shortrefs[key] = cmodel['layers'][key]['shortref']
        else:
            print('No shortref provided for layer %s' % key)
            shortrefs[key] = 'unknown'
    try:
        modelrefs['longref'] = cmodel['longref']
    except:
        print('No model longref provided')
        modelrefs['longref'] = 'unknown'
    try:
        modelrefs['shortref'] = cmodel['shortref']
    except:
        print('No model shortref provided')
        modelrefs['shortref'] = 'unknown'
    return modelrefs, longrefs, shortrefs


def checkTerm(term, layers):
    # startterm = term
    # Strip out everything that isn't: 0-9.() operators, +-/* or layer names.
    # Anything left is an unknown symbol.
    tterm = term
    # remove log, sqrt, etc.
    for op in OPERATORS:
        tterm = tterm.replace(op, '')
    # remove ShakeMap variables
    for sm_term in SM_TERMS:
        tterm = tterm.replace(sm_term, '')
    # remove layer names
    for layer in layers:
        tterm = tterm.replace(layer, '')
    # remove arithmetic operators
    tterm = re.sub(OPERATORPAT, '', tterm)
    # remove floating point numbers
    tterm = re.sub(FLOATPAT, '', tterm)
    # remove integer numbers
    tterm = re.sub(INTPAT, '', tterm)
    # remove parentheses
    tterm = re.sub('[()]*', '', tterm)
    # remove any blank spaces
    tterm = tterm.strip()
    # remove commas
    tterm = tterm.strip(',')
    # anything left *might* cause an error
    for op in OPERATORS:
        if term.find(op) > -1:
            term = term.replace(op, 'np.'+op)

    for sm_term in SM_GRID_TERMS:
        term = term.replace(
            sm_term,
            "self.shakemap.getSlice(rowstart, rowend, "
            "colstart, colend, name='%s')" % sm_term)

    # replace the macro MW with the magnitude value from the shakemap
    term = term.replace('MW', "self.shakemap.edict['magnitude']")

    # term.replace('YEAR',"self.shakemap.getEventDict()['event_time'].year")
    # hasTime = False
    timeField = None
    for unit in ['YEAR', 'MONTH', 'DAY', 'HOUR']:
        if term.find(unit) > -1:
            term = term.replace(unit, '')
            timeField = unit

    for layer in layers:
        if layer == 'friction':
            term = term.replace(
                layer,
                "np.nan_to_num(self.layerdict['%s'].getSlice(rowstart, "
                "rowend, colstart, colend, name='%s'))" % (layer, layer))
        else:
            term = term.replace(
                layer,
                "self.layerdict['%s'].getSlice(rowstart, rowend, colstart, "
                "colend, name='%s')" % (layer, layer))
    return (term, tterm, timeField)