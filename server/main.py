from fastapi import FastAPI, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware

import logging

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from model import predict, init
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import os

from server.dependency import Settings, PredictionException

WAIT_TIME = 10

settings = Settings()
app = FastAPI()
connected = False
shutdown = False
pool = ThreadPoolExecutor(10)

# Must have CORSMiddleware to enable localhost client and server
origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5057",
    "http://localhost:5000",
    "http://localhost:6379",
]

logger = logging.getLogger("api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PredictionException)
async def prediction_exception_handler(request: Request, exc: PredictionException):
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": 'failure',
            "detail": "Model is not ready to receive predictions."
        },
    )


def ping_server(server_port, model_port, model_name):
    """
    Periodically ping the server to make sure that
    it is active.
    """
    global connected
    while connected and not shutdown:
        try:
            r = requests.get('http://host.docker.internal:' + str(server_port) + '/')
            r.raise_for_status()
            time.sleep(WAIT_TIME)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            connected = False
            logger.debug("Server " + model_name + " is not responsive. Retry registering...")
    if not shutdown:
        register_model_to_server(server_port, model_port, model_name)


def register_model_to_server(server_port, model_port, model_name):
    """
    Send notification to the server with the model name and port to register the microservice
    It retries until a connection with the server is established
    """
    global connected
    while not connected and not shutdown:
        try:
            r = requests.post('http://host.docker.internal:' + str(server_port) + '/model/register',
                              json={"modelName": model_name, "modelPort": model_port})
            r.raise_for_status()
            connected = True
            logger.debug('Registering to server succeeds.')
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            logger.debug('Registering to server fails. Retry in ' + str(WAIT_TIME) + ' seconds')
            time.sleep(WAIT_TIME)
            continue
    if not shutdown:
        ping_server(server_port, model_port, model_name)


@app.get("/")
async def root():
    """
    Default endpoint for testing if the server is running
    :return: Positive JSON Message
    """
    return {"MLMicroserviceTemplate is Running!"}


@app.on_event("startup")
def initial_startup():
    """
    Calls the init() method in the model and prepares the model to receive predictions. The init
    task may take a long time to complete, so the settings field ready_to_predict will be updated
    asynchronously when init() completes.

    :return: {"result": "starting"}
    """
    # Run startup task async
    load_dotenv()

    # Register the model to the server in a separate thread to avoid meddling with
    # initializing the service which might be used directly by other client later on
    global background_server_connection
    background_server_connection = pool.submit(register_model_to_server, os.getenv('SERVER_PORT'), os.getenv('PORT'), os.getenv('NAME'))
    BackgroundTask(init)
    return {"result": "starting"}


@app.on_event('shutdown')
def on_shutdown():
    settings.ready_to_predict = False
    global shutdown
    shutdown = True
    pool.shutdown()

    return {
        'status': 'success',
        'detail': 'Deregister complete and server shutting down.',
    }


@app.get("/status")
async def check_status():
    """
    Checks the current prediction status of the model. Predictions are not able to be made
    until this method returns {"result": "True"}.

    :return: {"result": "True"} if model is ready for predictions, else {"result": "False"}
    """

    if not settings.ready_to_predict:
        raise PredictionException()

    return {
        'status': 'success',
        'detail': 'Model ready to receive prediction requests.'
    }


@app.post("/predict")
async def create_prediction(filename: str = ""):
    """
    Creates a new prediction using the model. This method must be called after the init() method has run
    at least once, otherwise this will fail with a HTTP Error. When given a filename, the server will create a
    file-like object of the image file and pass that to the predict() method.

    :param filename: Image file name for an image stored in shared Docker volume photoanalysisserver_images
    :return: JSON with field "result" containing the results of the model prediction.
    """

    # Ensure model is ready to receive prediction requests
    if not settings.ready_to_predict:
        return HTTPException(status_code=503,
                             detail="Model has not been configured. Please run initial startup before attempting to "
                                    "receive predictions.")

    # Attempt to open image file
    try:
        image_file = open('../images/' + filename, 'r')
    except IOError:
        return HTTPException(status_code=400,
                             detail="Unable to open image file. Provided filename can not be found on server.")

    # Create prediction with model
    result = predict(image_file)
    image_file.close()
    return {"result": result}