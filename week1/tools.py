import os
import fnmatch
from operator import itemgetter
import math
import re
import hashlib
import warnings
import glob
import six
from shutil import copy
import random


import numpy as np
from keras import backend as K
from keras.preprocessing import image
from keras.applications.vgg19 import preprocess_input
from keras.preprocessing.image import (ImageDataGenerator, Iterator,
                                       array_to_img, img_to_array, load_img)

MAX_NUM_IMAGES_PER_CLASS = 2 ** 27 - 1  # ~134M
VALID_IMAGE_FORMATS = frozenset(['jpg', 'jpeg', 'JPG', 'JPEG'])


def scaled_exp_decay(start: float, end: float, n_iter: int,
               current_iter: int) -> float:
    """ Exponentially modifies value from start to end.

    Args:
        start: float, initial value
        end: flot, final value
        n_iter: int, total number of iterations
        current_iter: int, current iteration
    """
    b = math.log(start/end, n_iter)
    a = start*math.pow(1, b)
    value = a/math.pow((current_iter+1), b)
    return value

def linear_decay(start: float, end: float, n_iter: int,
           current_iter: int) -> float:
    """ Linear modifies value from start to end.

    Args:
        start: float, initial value
        end: flot, final value
        n_iter: int, total number of iterations
        current_iter: int, current iteration
    """
    return (end - start)/n_iter*current_iter + start



def _get_fields(attr):
    if isinstance(attr, Config):
        return [getattr(attr, k) for k in
                sorted(attr.__dict__.keys())]
    else:
        return [attr]


class Config:

    def __init__(self, **kwargs):
        """ Init config class with local configurations provided via kwargs.
            Provide scope field to mark config as main.
        """

        for name, attr in kwargs.items():
            setattr(self, name, attr)

        if 'scope' in kwargs.keys():
            self.is_main = True

            # collect all fields from all configs and regular kwargs
            fields = (_get_fields(attr) for name, attr in
                      sorted(kwargs.items(), key=itemgetter(0))
                      if not name == "scope")

            self.identifier_fields = sum(fields, [])

    @property
    def identifier(self):
        if self.is_main:
            fields = "_".join(self._process_attr(name)
                              for name in self.identifier_fields)
            return self.scope + "_" + fields
        else:
            raise AttributeError("There is no field `scope` in this config")

    def _process_attr(self, attr):
        if isinstance(attr, (int, float, str)):
            return str(attr)
        elif isinstance(attr, (list, tuple)):
            return 'x'.join(str(a) for a in attr)
        elif isinstance(attr, bool):
            if attr:
                return 'YES{}'.format(str(attr))
            else:
                return 'NO{}'.format(str(attr))
        else:
            raise TypeError('Wrong dtype.')


def lr_scheduler(current_epoch, config):
    lr_min = config.train.lr_min
    lr_max = config.train.lr_max
    edge = config.train.epochs // 3
    if current_epoch < current_epoch:
        return linear_decay(lr_min, lr_max, edge, current_epoch)
    else:
        return linear_decay(lr_max, lr_min, config.train.epochs, current_epoch)

def find_files(path: str, filename_pattern: str, sort: bool = True) -> list:
    """Finds all files of type `filename_pattern`
    in directory and subdirectories paths.

    Args:
        path: str, directory to search files.
        filename_pattern: regular expression to specify file type.
        sort: bool, whether to sort files list. Defaults to True.

    Returns: list of found files.
    """
    files = list()
    for root, _, filenames in os.walk(path):
        for filename in fnmatch.filter(filenames, filename_pattern):
            files.append(os.path.join(root, filename))
    if sort:
        files.sort()
    return files

def read_dirs_names(path):
    """ Return list with dirs names for provided path. """
    return [name for name in os.listdir(path) if os.path.isdir(name)]

############################# DATA PROCESSING ##################################
def preprocess_img(img):
    """ Load and preprocess one image. """
    img = image.img_to_array(image.load_img(img, target_size=(224, 224)))
    img = preprocess_input(img)
    return img
################################################################################


# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/examples/image_retraining/retrain.py
def create_image_lists(image_dir, validation_pct=10):
    """Builds a list of training images from the file system.

    Analyzes the sub folders in the image directory, splits them into stable
    training, testing, and validation sets, and returns a data structure
    describing the lists of images for each label and their paths.

    # Arguments
        image_dir: string path to a folder containing subfolders of images.
        validation_pct: integer percentage of images reserved for validation.

    # Returns
        dictionary of label subfolder, with images split into training
        and validation sets within each label.
    """
    if not os.path.isdir(image_dir):
        raise ValueError("Image directory {} not found.".format(image_dir))
    image_lists = {}
    sub_dirs = [x[0] for x in os.walk(image_dir)]
    sub_dirs_without_root = sub_dirs[1:]  # first element is root directory
    for sub_dir in sub_dirs_without_root:
        file_list = []
        dir_name = os.path.basename(sub_dir)
        if dir_name == image_dir:
            continue
        # print("Looking for images in '{}'".format(dir_name))
        for extension in VALID_IMAGE_FORMATS:
            file_glob = os.path.join(image_dir, dir_name, '*.' + extension)
            file_list.extend(glob.glob(file_glob))
        if not file_list:
            warnings.warn('No files found')
            continue
        if len(file_list) < 20:
            warnings.warn('Folder has less than 20 images, which may cause '
                          'issues.')
        elif len(file_list) > MAX_NUM_IMAGES_PER_CLASS:
            warnings.warn('WARNING: Folder {} has more than {} images. Some '
                          'images will never be selected.'
                          .format(dir_name, MAX_NUM_IMAGES_PER_CLASS))
        label_name = re.sub(r'[^a-z0-9]+', ' ', dir_name.lower())
        training_images = []
        validation_images = []
        for file_name in file_list:
            base_name = os.path.basename(file_name)
            # Get the hash of the file name and perform variant assignment.
            hash_name = hashlib.sha1(as_bytes(base_name)).hexdigest()
            hash_pct = ((int(hash_name, 16) % (MAX_NUM_IMAGES_PER_CLASS + 1)) *
                        (100.0 / MAX_NUM_IMAGES_PER_CLASS))
            if hash_pct < validation_pct:
                validation_images.append(base_name)
            else:
                training_images.append(base_name)
        image_lists[label_name] = {
            'dir': dir_name,
            'training': training_images,
            'validation': validation_images,
        }
    return image_lists


def as_bytes(bytes_or_text, encoding='utf-8'):
    """Converts bytes or unicode to `bytes`, using utf-8 encoding for text.

    # Arguments
        bytes_or_text: A `bytes`, `str`, or `unicode` object.
        encoding: A string indicating the charset for encoding unicode.

    # Returns
        A `bytes` object.

    # Raises
        TypeError: If `bytes_or_text` is not a binary or unicode string.
    """
    if isinstance(bytes_or_text, six.text_type):
        return bytes_or_text.encode(encoding)
    elif isinstance(bytes_or_text, bytes):
        return bytes_or_text
    else:
        raise TypeError('Expected binary or unicode string, got %r' %
                        (bytes_or_text,))

def get_generators(image_lists, config):
    def except_catcher(gen):
        while True:
            try:
                data = next(gen)
                yield data
            except Exception as e:
                print('Ups! Something wrong!', e)
    
    def normilize(img):
        return (img/255 - 0.5)*2

    train_datagen = CustomImageDataGenerator(rotation_range=45,
                                             width_shift_range=0.2,
                                             height_shift_range=0.2,
                                             zoom_range=0.2,
                                             channel_shift_range=0.1,
                                             horizontal_flip=True,
                                             vertical_flip=True,
                                             preprocessing_function=normilize)

    test_datagen = CustomImageDataGenerator(preprocessing_function=normilize)

    train_generator = train_datagen.flow_from_image_lists(
        image_lists=image_lists,
        category='training',
        image_dir=config.data.path_to_data,
        target_size=(config.data.img_height, config.data.img_width),
        batch_size=config.data.batch_size,
        class_mode='categorical')

    validation_generator = test_datagen.flow_from_image_lists(
        image_lists=image_lists,
        category='validation',
        image_dir=config.data.path_to_data,
        target_size=(config.data.img_height, config.data.img_width),
        batch_size=config.data.batch_size,
        class_mode='categorical')

    return except_catcher(train_generator), except_catcher(validation_generator)

def get_classes_map(path_to_data):
    paths_to_classes = [os.path.join(path_to_data, name) for name in os.listdir(path_to_data)]
    paths_to_classes = [name for name in paths_to_classes if os.path.isdir(name)]
    classes = [path.split('/')[-1] for path in paths_to_classes]
    classes.sort()
    return {i: class_ for i, class_ in enumerate(classes)}

def copy_images(config):
    paths_to_classes = [os.path.join(config.data.path_to_data, name) for name in os.listdir(config.data.path_to_data)]
    paths_to_classes = [name for name in paths_to_classes if os.path.isdir(name)]
    for path in paths_to_classes:
        print(path)
        dir_name = path.split('/')[-1]
        copy_to_train = os.path.join('./autoria/train', dir_name)
        copy_to_test = os.path.join('./autoria/test', dir_name)
        print(copy_to_train)
        print(copy_to_test)
        os.makedirs(copy_to_train, exist_ok=True)
        os.makedirs(copy_to_test, exist_ok=True)
        paths_to_img = find_files(path, '*.jpg')
        random.shuffle(paths_to_img)
        edge = int(len(paths_to_img)*config.data.valid_size)
        train_paths = paths_to_img[edge:]
        val_paths = paths_to_img[:edge]

        for img in train_paths:
            copy(img, copy_to_train)
        
        for img in val_paths:
            copy(img, copy_to_test)
        
        print(path, 'done')
    print('all done')



def get_generators_standart(config):
    def except_catcher(gen):
        while True:
            try:
                data = next(gen)
                yield data
            except Exception as e:
                print('Ups! Something wrong!', e)
    
    def normilize(img):
        return (img/255 - 0.5)*2

    train_datagen = ImageDataGenerator(rotation_range=45,
                                        width_shift_range=0.2,
                                        height_shift_range=0.2,
                                        zoom_range=0.2,
                                        horizontal_flip=True,
                                        vertical_flip=True,
                                        preprocessing_function=normilize)

    test_datagen = ImageDataGenerator(preprocessing_function=normilize)

    train_generator = train_datagen.flow_from_directory(
        directory=config.data.path_to_train,
        target_size=(config.data.img_height, config.data.img_width),
        batch_size=config.data.batch_size,
        class_mode='categorical')

    validation_generator = test_datagen.flow_from_directory(
        directory=config.data.path_to_test,
        target_size=(config.data.img_height, config.data.img_width),
        batch_size=config.data.batch_size,
        class_mode='categorical')

    return except_catcher(train_generator), except_catcher(validation_generator)

class CustomImageDataGenerator(ImageDataGenerator):
    def flow_from_image_lists(self, image_lists,
                              category, image_dir,
                              target_size=(256, 256), color_mode='rgb',
                              class_mode='categorical',
                              batch_size=32, shuffle=True, seed=None,
                              save_to_dir=None,
                              save_prefix='',
                              save_format='jpeg'):
        return ImageListIterator(
            image_lists, self,
            category, image_dir,
            target_size=target_size, color_mode=color_mode,
            class_mode=class_mode,
            data_format=self.data_format,
            batch_size=batch_size, shuffle=shuffle, seed=seed,
            save_to_dir=save_to_dir,
            save_prefix=save_prefix,
            save_format=save_format)


class ImageListIterator(Iterator):
    """Iterator capable of reading images from a directory on disk.

    # Arguments
        image_lists: Dictionary of training images for each label.
        image_data_generator: Instance of `ImageDataGenerator`
            to use for random transformations and normalization.
        target_size: tuple of integers, dimensions to resize input images to.
        color_mode: One of `"rgb"`, `"grayscale"`. Color mode to read images.
        classes: Optional list of strings, names of sudirectories
            containing images from each class (e.g. `["dogs", "cats"]`).
            It will be computed automatically if not set.
        class_mode: Mode for yielding the targets:
            `"binary"`: binary targets (if there are only two classes),
            `"categorical"`: categorical targets,
            `"sparse"`: integer targets,
            `None`: no targets get yielded (only input images are yielded).
        batch_size: Integer, size of a batch.
        shuffle: Boolean, whether to shuffle the data between epochs.
        seed: Random seed for data shuffling.
        data_format: String, one of `channels_first`, `channels_last`.
        save_to_dir: Optional directory where to save the pictures
            being yielded, in a viewable format. This is useful
            for visualizing the random transformations being
            applied, for debugging purposes.
        save_prefix: String prefix to use for saving sample
            images (if `save_to_dir` is set).
        save_format: Format to use for saving sample images
            (if `save_to_dir` is set).
    """

    def __init__(self, image_lists, image_data_generator,
                 category, image_dir,
                 target_size=(256, 256), color_mode='rgb',
                 class_mode='categorical',
                 batch_size=32, shuffle=True, seed=None,
                 data_format=None,
                 save_to_dir=None, save_prefix='', save_format='jpeg'):
        if data_format is None:
            data_format = K.image_data_format()

        classes = list(image_lists.keys())
        self.category = category
        self.num_class = len(classes)
        self.image_lists = image_lists
        self.image_dir = image_dir

        how_many_files = 0
        for label_name in classes:
            for _ in self.image_lists[label_name][category]:
                how_many_files += 1

        self.samples = how_many_files
        self.class2id = dict(zip(classes, range(len(classes))))
        self.id2class = dict((v, k) for k, v in self.class2id.items())
        self.classes = np.zeros((self.samples,), dtype='int32')

        self.image_data_generator = image_data_generator
        self.target_size = tuple(target_size)
        if color_mode not in {'rgb', 'grayscale'}:
            raise ValueError('Invalid color mode:', color_mode,
                             '; expected "rgb" or "grayscale".')
        self.color_mode = color_mode
        self.data_format = data_format
        if self.color_mode == 'rgb':
            if self.data_format == 'channels_last':
                self.image_shape = self.target_size + (3,)
            else:
                self.image_shape = (3,) + self.target_size
        else:
            if self.data_format == 'channels_last':
                self.image_shape = self.target_size + (1,)
            else:
                self.image_shape = (1,) + self.target_size

        if class_mode not in {'categorical', 'binary', 'sparse', None}:
            raise ValueError('Invalid class_mode:', class_mode,
                             '; expected one of "categorical", '
                             '"binary", "sparse", or None.')
        self.class_mode = class_mode
        self.save_to_dir = save_to_dir
        self.save_prefix = save_prefix
        self.save_format = save_format

        i = 0
        self.filenames = []
        for label_name in classes:
            for j, _ in enumerate(self.image_lists[label_name][category]):
                self.classes[i] = self.class2id[label_name]
                img_path = get_image_path(self.image_lists,
                                          label_name,
                                          j,
                                          self.image_dir,
                                          self.category)
                self.filenames.append(img_path)
                i += 1

        # print("Found {} {} files".format(len(self.filenames), category))
        super(ImageListIterator, self).__init__(self.samples, batch_size, shuffle,
                                                seed)

    def next(self):
        """For python 2.x.

        # Returns
            The next batch.
        """
        with self.lock:
            index_array, current_index, current_batch_size = next(
                self.index_generator)
        # The transformation of images is not under thread lock
        # so it can be done in parallel
        batch_x = np.zeros((current_batch_size,) + self.image_shape,
                           dtype=K.floatx())
        grayscale = self.color_mode == 'grayscale'
        # build batch of image data
        for i, j in enumerate(index_array):
            img = load_img(self.filenames[j],
                           grayscale=grayscale,
                           target_size=self.target_size)
            x = img_to_array(img, data_format=self.data_format)
            x = self.image_data_generator.random_transform(x)
            x = self.image_data_generator.standardize(x)
            batch_x[i] = x
        # optionally save augmented images to disk for debugging purposes
        if self.save_to_dir:
            for i in range(current_batch_size):
                img = array_to_img(batch_x[i], self.data_format, scale=True)
                fname = '{prefix}_{index}_{hash}.{format}'.format(
                    prefix=self.save_prefix,
                    index=current_index + i,
                    hash=np.random.randint(10000),
                    format=self.save_format)
                img.save(os.path.join(self.save_to_dir, fname))
        # build batch of labels
        if self.class_mode == 'sparse':
            batch_y = self.classes[index_array]
        elif self.class_mode == 'binary':
            batch_y = self.classes[index_array].astype(K.floatx())
        elif self.class_mode == 'categorical':
            batch_y = np.zeros((len(batch_x), self.num_class),
                               dtype=K.floatx())
            for i, label in enumerate(self.classes[index_array]):
                batch_y[i, label] = 1.
        else:
            return batch_x
        return batch_x, batch_y


# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/examples/image_retraining/retrain.py
def get_image_path(image_lists, label_name, index, image_dir, category):
    """"Returns a path to an image for a label at the given index.

    # Arguments
      image_lists: Dictionary of training images for each label.
      label_name: Label string we want to get an image for.
      index: Int offset of the image we want. This will be moduloed by the
      available number of images for the label, so it can be arbitrarily large.
      image_dir: Root folder string of the subfolders containing the training
      images.
      category: Name string of set to pull images from - training, testing, or
      validation.

    # Returns
      File system path string to an image that meets the requested parameters.
    """
    if label_name not in image_lists:
        raise ValueError('Label does not exist ', label_name)
    label_lists = image_lists[label_name]
    if category not in label_lists:
        raise ValueError('Category does not exist ', category)
    category_list = label_lists[category]
    if not category_list:
        raise ValueError('Label %s has no images in the category %s.',
                         label_name, category)
    mod_index = index % len(category_list)
    base_name = category_list[mod_index]
    sub_dir = label_lists['dir']
    full_path = os.path.join(image_dir, sub_dir, base_name)
    return full_path

def mock_generator(config):
    while True:
        img = np.random.random([config.data.batch_size, 224, 224, 3])
        targets = np.zeros([config.data.batch_size, 1000])
        targets[:, 0] = 1
        yield (img, targets)