
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
from Preprocess import *
import cv2
import time

f = h5py.File("multiModel.h5", mode='r')
model_version = f.attrs.get('keras_version')
keras_version = str(keras_version).encode('utf8')

if model_version != keras_version:
    print('You are using Keras version ', keras_version, ', but the model was built using ', model_version)


def customLoss(y_true, y_pred):
    return K.mean(K.square(y_pred - y_true), axis=-1)

model = load_model("multiModel.h5", custom_objects={'customLoss':customLoss})
graph = tf.get_default_graph()


def copyImage(byte_array, imageSize):
    if imageSize > 8:
        resize(byte_array, imageSize)
        image = []
        for i in range(imageSize):
            image.append(byte_array[i])
        return array.array('B', image).tostring()
    return byte_array


def imageReceived(imageSize, rawImage):
	global graph
	t = time.time()
	with graph.as_default():
		jpegImage = copyImage(rawImage, imageSize)
		print("Copy Image", time.time() - t)
		t = time.time()
		image = Image.open(BytesIO(jpegImage))
		print("Open Image", time.time() - t)
		t = time.time()
		# image.save('test.jpg')
		# image = cv2.imread('test.jpg', 1)
		image_array = np.asarray(image)
		image_array = cv2.resize(image_array, (320, 160))
		print("Resize Image", time.time() - t)
		t = time.time()
		steering_angle, throttle, brake_value = model.predict([preprocessImage(image_array)[None,:,:,:]])
		print("Predict", time.time() - t)
		t = time.time()
		Node.steerCommand(c_float(steering_angle))
		Node.brakeCommand(c_float(brake_value))
		print("Commands sent", time.time() - t)
		Node.throttleCommand(c_float(throttle))


Node = MainNode(imageReceived)
Node.connectPolySync()
