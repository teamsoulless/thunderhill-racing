from MainNode.MainNode import MainNode
from ctypes import *
import array
from PIL import Image
from io import BytesIO
import numpy as np
from keras.models import load_model
import h5py
from keras import __version__ as keras_version
import tensorflow as tf
from keras import backend as K
from keras.models import model_from_json, load_model
from keras.preprocessing.image import ImageDataGenerator, array_to_img, img_to_array
from keras.optimizers import Adam
from keras import backend as K
import tensorflow as tf
import cv2
import time
from data_buffer import DataBuffer
import queue
import threading
import json


f = h5py.File("psyncPosNet.h5", mode='r')
model_version = f.attrs.get('keras_version')
keras_version = str(keras_version).encode('utf8')

if model_version != keras_version:
    print('You are using Keras version ', keras_version,
          ', but the model was built using ', model_version)


def customLoss(y_true, y_pred):
    return K.mean(K.square(y_pred - y_true), axis=-1)

model = load_model("psyncPosNet.h5", custom_objects={'customLoss': customLoss})
graph = tf.get_default_graph()

data_buffer = DataBuffer()
res_queue = queue.Queue(maxsize=1)

idxs = [0, 1, 2]
means = [-2.135234308696, 0.690051203865, 62.68238949]
stds = [0.000022089013, 0.000045442627, 13.48539298]

debug = False

# Deprecated used to normalize speed and throttle
# def normalize_vector(xVec):
#	for i, mean, std in zip(idxs, means, stds):
#		xVec[i] -= mean
#		xVec[i] /= std
#	return xVec

##
# This function copies the image as a byte array using the ctype library to then feed into the model as a jpeg image
##


def copyImage(byte_array, imageSize):
    if imageSize > 8:
        resize(byte_array, imageSize)
        image = []
        for i in range(imageSize):
            image.append(byte_array[i])
        return array.array('B', image).tostring()
    return byte_array

##
# Takes in a recieved raw image and adds it into our data buffer
##


def imageReceived(imageSize, rawImage, speed, lat, lon):
    print("image received with: ", speed, lat, lon)
    jpegImage = copyImage(rawImage, imageSize)
    data_buffer.add_item((jpegImage, speed, lat, lon))

##
# This is polysync's main communication note to recieve images, brake, throttle, etc..
##
Node = MainNode(imageReceived)

X = []
Y = []
model = None
tf_session = None
tf_graph = None
training_started = False

##
# Sending predictions to the model
##


def make_prediction():
    global graph
    global model
    global tf_session
    global tf_graph
    global training_started
    print('make prediction')

    # recording flag is replaced with training flag to start image data
    # collection
    training = True
    # To be able to feed batches of data continously
    while True:
        with tf_session.as_default():
            with tf_graph.as_default():
                item = data_buffer.get_item_for_processing()
                if item and len(item) == 4:
                    jpeg_image = item[0]
                    speed = item[1]
                    lat = item[2]
                    lon = item[3]

                    if jpeg_image:
                        image = np.array(Image.frombytes(
                            'RGB', [960, 480], jpeg_image, 'raw'))
                        image_array = np.asarray(image)
                        image_array = cv2.cvtColor(image_array, cv2.COLOR_BGR2RGB)
                        image_array = cv2.resize(image_array, (160, 80))
                        transformed_image_array = image_array[None, :, :, :]
                        # only start predicting if we have training active;
                        # otherwise, trainer will not start up
                        if training_started:
                            prediction = model.predict(
                                transformed_image_array, batch_size=1)[0]
                            steering_angle = float(prediction[0])
                        else:
                            steering_angle = 0.0
                        # float(prediction[1]) # At the moment we are not using the
                        # throttle value from polysync
                        throttle = 0.0
                        # float(prediction[2]) # At the moment we are not using the
                        # brake value from polysync
                        brake = 0.0

                        if brake > 0.5:
                            throttle = -brake

                        if res_queue.full():  # maintain a single most recent prediction in the queu
                            res_queue.get(False)

                        # save only steering predictions
                        res_queue.put((steering_angle, throttle, brake))

                        # fill the training and testing queue
                        if len(X) < 120:
                            X.append(transformed_image_array)
                            Y.append(steering_angle)
                        else:
                            if len(X) == 120:
                                print("Ready for training!")

                        # If we are training then append the X and Y values to feed
                        # to the model
                        if training and len(transformed_image_array) > 0:
                            X.append(transformed_image_array)
                            Y.append(steering_angle)

##
# Send predicted values to Polysync to feed into the car
##


def sendValues():
    steer = 0.0
    throttle = 0.0
    brake = 0.0
    while 1:
        try:
            prediction = res_queue.get(block=False)
            print(prediction)
            steer = float(prediction[0])
            throttle = float(prediction[1])
            brake = float(prediction[2])
            print("got values: ", steer, throttle, brake)
        except queue.Empty:
            pass

        # use cruise control
        Node.steerCommand(steer)
        Node.throttleCommand(throttle)
        Node.brakeCommand(brake)
        time.sleep(0.01)

##
# Batch X and Y values, images and steering in this case
##


def batchgen(X, Y):
    global training_started
    while 1:
        y = Y[i]
        image = X[i]
        y = np.array([[y]])
        training_started = True
        yield image, y

##
#  Loads already trained model weights to be able to retrain from realtime data
##
def model_trainer(fileModelJSON, tf_session, tf_graph):
    global model
    print("Model Trainer Thread Starting...")
    fileWeights = fileModelJSON.replace('json', 'h5')
    with tf_session.as_default():
        with tf_graph.as_default():
            # NOTE: Need json model since using h5 combo will not allow you to change
            # optimizer nor learning rates.  The optimizer is `baked` into the h5 combo.
            with open(fileModelJSON, 'r') as jfile:
                model = model_from_json(json.load(jfile))
            adam = Adam(lr=0.00001, beta_1=0.9, beta_2=0.999, epsilon=1e-08, decay=0.0)
            model.compile(optimizer=adam, loss="mse")
            model.load_weights(fileWeights)
            model.summary()

            # start training loop...
            while 1:
                if len(X) > 100:
                    batch_size = 20
                    samples_per_epoch = int(len(X) / batch_size)
                    val_size = int(samples_per_epoch / 10)
                    if val_size < 10:
                        val_size = 10
                    nb_epoch = 100

                    history = tmodel.fit_generator(batchgen(X, Y),
                                                   samples_per_epoch=samples_per_epoch,
                                                   nb_epoch=nb_epoch,
                                                   validation_data=batchgen(X, Y),
                                                   nb_val_samples=val_size,
                                                   verbose=1)

                    print("Saving model to disk: ", fileModelJSON, "and", fileWeights)
                    if Path(fileModelJSON).is_file():
                        os.remove(fileModelJSON)
                    json_string = model.to_json()
                    with open(fileModelJSON,'w' ) as f:
                        json.dump(json_string, f)
                    if Path(fileWeights).is_file():
                        os.remove(fileWeights)
                    model.save_weights(fileWeights)
                else:
                    # Not ready for training yet
                    print("Not Ready!  Sleeping for 5...")
                    time.sleep(5)

##
# Our trainer has three parallel threads to train, predict and send values.
##
if __name__ == '__main__':
    # Getting values from polysync to make predictions thread
    thread = threading.Thread(target=make_prediction, args=())
    thread.daemon = True
    thread.start()

    # Sending values to Polysync thread
    thread2 = threading.Thread(target=sendValues, args=())
    thread2.daemon = True
    thread2.start()

    # start training models thread
    tf_session = K.get_session()
    tf_graph = tf.get_default_graph()
    thread3 = threading.Thread(target=model_trainer, args=('modelTrained.json', tf_session, tf_graph))
    thread3.daemon = True
    thread3.start()

    # Connect to Polysync to render those threads
    Node.connectPolySync()
