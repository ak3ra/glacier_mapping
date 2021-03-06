#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
# pylint: disable=E1137,E1136,E0110,E1101
from Datasets import load_datasets, get_area_from_geometry
from Session import Session, manage_session_folders, SESSION_FOLDER
from SessionHandler import SessionHandler
from addict import Dict
from log import setup_logging, LOGGER
import DataLoader as DL
import Utils as utils
import argparse
import beaker.middleware
import bottle
import cheroot.wsgi
import cv2
import fiona
import fiona.transform
import joblib
import json
import numpy as np
import os
import rasterio
import rasterio.warp
import sys

DATASETS = load_datasets()
ROOT_DIR = os.environ["WEBTOOL_ROOT"]
SESSION_HANDLER = None
bottle.TEMPLATE_PATH.insert(0, ROOT_DIR + "/views") # let bottle know where we are storing the template files

#---------------------------------------------------------------------------------------
#---------------------------------------------------------------------------------------


def setup_sessions():
    '''This method is called before every request. Adds the beaker SessionMiddleware on as request.session.
    '''
    bottle.request.session = bottle.request.environ['beaker.session']
    bottle.request.client_ip = bottle.request.environ.get('HTTP_X_FORWARDED_FOR') or bottle.request.environ.get('REMOTE_ADDR')


def manage_sessions():
    '''This method is called before every request. Checks to see if there a session associated with the current request.
    If there is then update the last interaction time on that session.
    '''
    if SESSION_HANDLER.is_expired(bottle.request.session.id): # Someone is trying to use a session that we have deleted due to inactivity
        SESSION_HANDLER.cleanup_expired_session(bottle.request.session.id)
        bottle.request.session.delete() # TODO: I'm not sure how the actual session is deleted on the client side
        LOGGER.info("Cleaning up an out of date session")
    elif not SESSION_HANDLER.is_active(bottle.request.session.id):
        LOGGER.warning("We are getting a request that doesn't have an active session")
    else:
        SESSION_HANDLER.touch_session(bottle.request.session.id) # let the SESSION_HANDLER know that this session has activity


#---------------------------------------------------------------------------------------
#---------------------------------------------------------------------------------------


def enable_cors():
    '''From https://gist.github.com/richard-flosi/3789163

    This globally enables Cross-Origin Resource Sharing (CORS) headers for every response from this server.
    '''
    bottle.response.headers['Access-Control-Allow-Origin'] = '*'
    bottle.response.headers['Access-Control-Allow-Methods'] = 'PUT, GET, POST, DELETE, OPTIONS'
    bottle.response.headers['Access-Control-Allow-Headers'] = 'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'


def do_options():
    '''This method is necessary for CORS to work (I think --Caleb)
    '''
    bottle.response.status = 204
    return


#---------------------------------------------------------------------------------------
#---------------------------------------------------------------------------------------


def create_session():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json

    SESSION_HANDLER.create_session(bottle.request.session.id, data["model"])

    bottle.response.status = 200
    return json.dumps(data)


def kill_session():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json

    SESSION_HANDLER.kill_session(bottle.request.session.id)
    SESSION_HANDLER.cleanup_expired_session(bottle.request.session.id)
    bottle.request.session.delete()

    bottle.response.status = 200
    return json.dumps(data)


def do_load():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json

    cached_model = data["cachedModel"]

    SESSION_HANDLER.get_session(bottle.request.session.id).reset(False, from_cached=cached_model)
    SESSION_HANDLER.get_session(bottle.request.session.id).load(cached_model)

    data["message"] = "Loaded new model from %s" % (cached_model)
    data["success"] = True

    bottle.response.status = 200
    return json.dumps(data)


def reset_model():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json
    data["remote_address"] = bottle.request.client_ip

    initial_reset = data.get("initialReset", False)
    if not initial_reset:
        SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction
        SESSION_HANDLER.get_session(bottle.request.session.id).save(data["experiment"])

    SESSION_HANDLER.get_session(bottle.request.session.id).reset()

    data["message"] = "Reset model"
    data["success"] = True

    bottle.response.status = 200
    return json.dumps(data)


def retrain_model():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json
    data["remote_address"] = bottle.request.client_ip

    success, message = SESSION_HANDLER.get_session(bottle.request.session.id).model.retrain(**data["retrainArgs"])

    if success:
        bottle.response.status = 200
        encoded_model_fn = SESSION_HANDLER.get_session(bottle.request.session.id).save(data["experiment"])
        data["cached_model"] = encoded_model_fn
        SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction
    else:
        data["error"] = message
        bottle.response.status = 500

    data["message"] = message
    data["success"] = success

    return json.dumps(data)


def record_correction():
    bottle.response.content_type = 'application/json'
    data = bottle.request.json
    data["remote_address"] = bottle.request.client_ip

    SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction

    #
    tlat, tlon = data["extent"]["ymax"], data["extent"]["xmin"]
    blat, blon = data["extent"]["ymin"], data["extent"]["xmax"]
    class_list = data["classes"]
    name_list = [item["name"] for item in class_list]
    class_idx = data["value"] # what we want to switch the class to
    origin_crs = "epsg:%d" % (data["extent"]["spatialReference"]["latestWkid"])

    # record points in lat/lon
    xs, ys = fiona.transform.transform(origin_crs, "epsg:4326", [tlon], [tlat])

    #
    naip_crs, naip_transform, naip_index = SESSION_HANDLER.get_session(bottle.request.session.id).current_transform

    xs, ys = fiona.transform.transform(origin_crs, naip_crs.to_dict(), [tlon,blon], [tlat,blat])

    tdst_x = xs[0]
    tdst_y = ys[0]
    tdst_col, tdst_row = (~naip_transform) * (tdst_x, tdst_y)
    tdst_row = int(np.floor(tdst_row))
    tdst_col = int(np.floor(tdst_col))

    bdst_x = xs[1]
    bdst_y = ys[1]
    bdst_col, bdst_row = (~naip_transform) * (bdst_x, bdst_y)
    bdst_row = int(np.floor(bdst_row))
    bdst_col = int(np.floor(bdst_col))

    tdst_row, bdst_row = min(tdst_row, bdst_row), max(tdst_row, bdst_row)
    tdst_col, bdst_col = min(tdst_col, bdst_col), max(tdst_col, bdst_col)

    SESSION_HANDLER.get_session(bottle.request.session.id).model.add_sample(tdst_row, bdst_row, tdst_col, bdst_col, class_idx)
    num_corrected = (bdst_row-tdst_row) * (bdst_col-tdst_col)

    data["message"] = "Successfully submitted correction"
    data["success"] = True
    data["count"] = num_corrected

    bottle.response.status = 200
    return json.dumps(data)


def do_undo():
    ''' Method called for POST `/doUndo`
    '''
    bottle.response.content_type = 'application/json'
    data = bottle.request.json
    data["remote_address"] = bottle.request.client_ip

    SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction

    # Forward the undo command to the backend model
    success, message, num_undone = SESSION_HANDLER.get_session(bottle.request.session.id).model.undo()
    data["message"] = message
    data["success"] = success
    data["count"] = num_undone

    bottle.response.status = 200
    return json.dumps(data)


def pred_patch():
    ''' Method called for POST `/predPatch`'''
    bottle.response.content_type = 'application/json'
    data = Dict(bottle.request.json)
    data["remote_address"] = bottle.request.client_ip

    SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction

    # Inputs
    extent = data.extent
    dataset = data.dataset
    name_list = [item.name for item in dataset.class_list]

    # Load the input data sources for the given tile
    if dataset.metadata.id not in DATASETS:
        raise ValueError("Dataset doesn't seem to be valid, do the datasets in js/tile_layers.js correspond to those in TileLayers.py")

    loaded_query = DATASETS[dataset.metadata.id]["data_loader"].get_data_from_extent(extent)
    SESSION_HANDLER.get_session(bottle.request.session.id).current_transform = (loaded_query["src_crs"], loaded_query["src_transform"])

    #   Run a model on the input data
    model = SESSION_HANDLER.get_session(bottle.request.session.id).model
    output = model.run(loaded_query["src_img"], extent, False)
    loaded_query["src_img"] = None # save memory
    assert len(output.shape) == 3, "The model function should return an image shaped as (height, width, num_classes)"
    assert (output.shape[2] < output.shape[0] and output.shape[2] < output.shape[1]), "The model function should return an image shaped as (height, width, num_classes)" # assume that num channels is less than img dimensions

    #   Warp output to EPSG:3857
    output, output_bounds = DL.warp_data_to_3857(
        output,
        loaded_query["src_crs"],
        loaded_query["src_transform"],
        loaded_query["src_bounds"]
    )

    # ------------------------------------------------------
    # Step 5
    #   Convert images to base64 and return
    # ------------------------------------------------------
    img_soft = np.round(utils.class_prediction_to_img(output)).astype(np.uint8)
    data["output_soft"] = DL.encode_rgb(img_soft)
    bottle.response.status = 200
    return json.dumps(data)


def pred_tile():
    ''' Method called for POST `/predTile`'''
    bottle.response.content_type = 'application/json'
    data = bottle.request.json
    data["remote_address"] = bottle.request.client_ip

    SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction

    # Inputs
    geom = data["polygon"]
    class_list = data["classes"]
    name_list = [item["name"] for item in class_list]
    dataset = data["dataset"]
    zone_layer_name = data["zoneLayerName"]

    if dataset not in DATASETS:
        raise ValueError("Dataset doesn't seem to be valid, do the datasets in js/tile_layers.js correspond to those in TileLayers.py")

    try:
        naip_data, raster_profile, raster_transform, raster_bounds, raster_crs = DATASETS[dataset]["data_loader"].get_data_from_shape(geom["geometry"])
        naip_data = np.rollaxis(naip_data, 0, 3)
        shape_area = get_area_from_geometry(geom["geometry"])
    except NotImplementedError as e:
        bottle.response.status = 400
        return json.dumps({"error": "Cannot currently download imagery with 'Basemap' based datasets"})

    output = SESSION_HANDLER.get_session(bottle.request.session.id).model.run(naip_data, geom, True)
    output_hard = output.argmax(axis=2)
    print("Finished, output dimensions:", output.shape)

    # apply nodata mask from naip_data
    nodata_mask = np.sum(naip_data == 0, axis=2) == naip_data.shape[2]
    output_hard[nodata_mask] = 255
    vals, counts = np.unique(output_hard[~nodata_mask], return_counts=True)

    # ------------------------------------------------------
    # Step 4
    #   Convert images to base64 and return
    # ------------------------------------------------------
    tmp_id = utils.get_random_string(8)
    img_hard = np.round(utils.class_prediction_to_img(output * 255,0)).astype(np.uint8)
    img_hard = cv2.cvtColor(img_hard, cv2.COLOR_RGB2BGRA)
    img_hard[nodata_mask] = [0,0,0,0]

    img_hard, img_hard_bounds = DL.warp_data_to_3857(img_hard, raster_crs, raster_transform, raster_bounds, resolution=10)

    cv2.imwrite(os.path.join(ROOT_DIR, "downloads/%s.png" % (tmp_id)), img_hard)
    data["downloadPNG"] = "downloads/%s.png" % (tmp_id)

    new_profile = raster_profile.copy()
    new_profile['driver'] = 'GTiff'
    new_profile['dtype'] = 'uint8'
    new_profile['compress'] = "lzw"
    new_profile['count'] = 1
    new_profile['transform'] = raster_transform
    new_profile['height'] = naip_data.shape[0]
    new_profile['width'] = naip_data.shape[1]
    new_profile['nodata'] = 255
    f = rasterio.open(os.path.join(ROOT_DIR, "downloads/%s.tif" % (tmp_id)), 'w', **new_profile)
    f.write(output_hard.astype(np.uint8), 1)
    f.close()
    data["downloadTIFF"] = "downloads/%s.tif" % (tmp_id)

    f = open(os.path.join(ROOT_DIR, "downloads/%s.txt" % (tmp_id)), "w")
    f.write("Class id\tClass name\tPercent area\tArea (km^2)\n")
    for i in range(len(vals)):
        pct_area = (counts[i] / np.sum(counts))
        if shape_area is not None:
            real_area = shape_area * pct_area
        else:
            real_area = -1
        f.write("%d\t%s\t%0.4f%%\t%0.4f\n" % (vals[i], name_list[vals[i]], pct_area*100, real_area))
    f.close()
    data["downloadStatistics"] = "downloads/%s.txt" % (tmp_id)

    bottle.response.status = 200
    return json.dumps(data)


def get_input():
    ''' Method called for POST `/getInput`
    '''
    bottle.response.content_type = 'application/json'
    data = Dict(bottle.request.json)
    data["remote_address"] = bottle.request.client_ip

    SESSION_HANDLER.get_session(bottle.request.session.id).add_entry(data) # record this interaction

    # Inputs
    extent = data.extent
    data_id = data.dataset.metadata.id

    if data_id not in DATASETS:
        raise ValueError("Dataset doesn't seem to be valid, please check Datasets.py")

    loaded_query = DATASETS[data_id]["data_loader"].get_data_from_extent(extent)
    img_data, img_bounds = DL.warp_data_to_3857(**loaded_query)
    data["input_rgb"] = DL.encode_rgb(img_data[:, :, [6, 3, 1]])
    bottle.response.status = 200
    return json.dumps(data)

def whoami():
    return str(bottle.request.session) + " " + str(bottle.request.session.id)


#---------------------------------------------------------------------------------------
#---------------------------------------------------------------------------------------
def get_landing_page():
    return bottle.static_file("landing_page.html", root=ROOT_DIR + "/")

def get_favicon():
    return

def get_everything_else(filepath):
    return bottle.static_file(filepath, root=ROOT_DIR + "/")


#---------------------------------------------------------------------------------------
#---------------------------------------------------------------------------------------


def main():
    global SESSION_HANDLER
    parser = argparse.ArgumentParser(description="AI for Earth Land Cover")

    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debugging", default=False)

    # TODO: make sure the storage type is passed onto the Session objects
    parser.add_argument(
        '--storage_type',
        action="store", dest="storage_type", type=str,
        choices=["table", "file"],
        default=None
    )
    parser.add_argument("--storage_path", action="store", dest="storage_path", type=str, help="Path to directory where output will be stored", default=None)
    parser.add_argument("--host", action="store", dest="host", type=str, help="Host to bind to", default="0.0.0.0")
    parser.add_argument("--port", action="store", dest="port", type=int, help="Port to listen on", default=8080)

    subparsers = parser.add_subparsers(dest="subcommand", help='Help for subcommands') # TODO: If we use Python3.7 we can use the required keyword here
    parser_a = subparsers.add_parser('local', help='For running models on the local server')
    parser_b = subparsers.add_parser('remote', help='For running models with RPC calls')
    parser.add_argument("--remote_host", action="store", dest="remote_host", type=str, help="RabbitMQ host", default="0.0.0.0")
    parser.add_argument("--remote_port", action="store", dest="remote_port", type=int, help="RabbitMQ port", default=8080)

    args = parser.parse_args(sys.argv[1:])


    # create Session factory to use based on whether we are running locally or remotely
    run_local = None
    if args.subcommand == "local":
        print("Sessions will be spawned on the local machine")
        run_local = True
    elif args.subcommand == "remote":
        print("Sessions will be spawned remotely")
        run_local = False
    else:
        print("Must specify 'local' or 'remote' on command line")
        return
    SESSION_HANDLER = SessionHandler(run_local, args)
    SESSION_HANDLER.start_monitor()

    # Setup logging
    log_path = os.getcwd() + "/logs"
    setup_logging(log_path, "server") # TODO: don't delete logs


    # Setup the bottle server
    app = bottle.Bottle()

    app.add_hook("after_request", enable_cors)
    app.add_hook("before_request", setup_sessions)
    app.add_hook("before_request", manage_sessions) # before every request we want to check to make sure there are no session issues

    # API paths
    app.route("/predPatch", method="OPTIONS", callback=do_options) # TODO: all of our web requests from index.html fire an OPTIONS call because of https://stackoverflow.com/questions/1256593/why-am-i-getting-an-options-request-instead-of-a-get-request, we should fix this
    app.route('/predPatch', method="POST", callback=pred_patch)

    app.route("/predTile", method="OPTIONS", callback=do_options)
    app.route('/predTile', method="POST", callback=pred_tile)

    app.route("/getInput", method="OPTIONS", callback=do_options)
    app.route('/getInput', method="POST", callback=get_input)

    app.route("/recordCorrection", method="OPTIONS", callback=do_options)
    app.route('/recordCorrection', method="POST", callback=record_correction)

    app.route("/retrainModel", method="OPTIONS", callback=do_options)
    app.route('/retrainModel', method="POST", callback=retrain_model)

    app.route("/resetModel", method="OPTIONS", callback=do_options)
    app.route('/resetModel', method="POST", callback=reset_model)

    app.route("/doUndo", method="OPTIONS", callback=do_options)
    app.route("/doUndo", method="POST", callback=do_undo)

    app.route("/doLoad", method="OPTIONS", callback=do_options)
    app.route("/doLoad", method="POST", callback=do_load)

    app.route("/createSession", method="OPTIONS", callback=do_options)
    app.route("/createSession", method="POST", callback=create_session)

    app.route("/killSession", method="OPTIONS", callback=do_options)
    app.route("/killSession", method="POST", callback=kill_session)

    app.route("/whoami", method="GET", callback=whoami)

    # Content paths
    app.route("/", method="GET", callback=get_landing_page)
    app.route("/favicon.ico", method="GET", callback=get_favicon)
    app.route("/<filepath:re:.*>", method="GET", callback=get_everything_else)


    manage_session_folders()
    session_opts = {
        'session.type': 'file',
        'session.cookie_expires': 3000,
        'session.data_dir': SESSION_FOLDER,
        'session.auto': True
    }
    app = beaker.middleware.SessionMiddleware(app, session_opts)

    server = cheroot.wsgi.Server(
        (args.host, args.port),
        app
    )

    server.max_request_header_size = 2**13
    server.max_request_body_size = 2**27

    try:
        server.start()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
