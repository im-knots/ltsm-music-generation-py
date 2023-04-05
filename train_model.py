import os
import tensorflow as tf
from keras.layers import LSTM, Dense, TimeDistributed, Bidirectional, Input, Activation, Add, Lambda, Attention, LayerNormalization
from keras.models import Sequential
from keras import Model

# Parameters
read_local = False
model_directory = "model"
timesteps = 5000
n_mels = 128
epochs = 25
batch_size = 8
use_tpu = True
validation_split = 0.2

if read_local:
    tfrecord_file = "audio_data.tfrecord"
else:
    gcs_bucket = "gs://knots-audio-processing"
    tfrecord_file = os.path.join(gcs_bucket, "audio_data.tfrecord")

# Environment setup
print("Setting up the environment...")
if use_tpu:
    try:
        tpu = tf.distribute.cluster_resolver.TPUClusterResolver()  # TPU detection
        print('Running on TPU ', tpu.cluster_spec().as_dict()['worker'])
    except ValueError:
        raise BaseException('ERROR: Not connected to a TPU runtime; please see the previous cell in this notebook for instructions!')

    tf.config.experimental_connect_to_cluster(tpu)
    tf.tpu.experimental.initialize_tpu_system(tpu)
    strategy = tf.distribute.TPUStrategy(tpu)

else:
    gpus = tf.config.list_physical_devices('GPU')
    if gpus:
        strategy = tf.distribute.OneDeviceStrategy("GPU:0")
    else:
        strategy = tf.distribute.OneDeviceStrategy("CPU:0")

# Model building and training
print("Building and training the model...")
with strategy.scope():
    input_layer = Input(shape=(timesteps, n_mels))
    lstm1 = Bidirectional(LSTM(512, return_sequences=True, kernel_initializer='he_normal'))(input_layer)
    lstm1_ln = LayerNormalization()(lstm1)

    attention = Attention()([lstm1_ln, lstm1_ln])
    lstm2 = LSTM(1024, return_sequences=True, kernel_initializer='he_normal')(attention)
    lstm2_ln = LayerNormalization()(lstm2)

    # Residual connection
    lstm2_add = Add()([lstm1_ln, lstm2_ln])

    lstm3 = LSTM(512, return_sequences=True, kernel_initializer='he_normal')(lstm2_add)
    lstm3_ln = LayerNormalization()(lstm3)
    output_layer = TimeDistributed(Dense(n_mels, activation="linear"))(lstm3_ln)

    model = Model(inputs=input_layer, outputs=output_layer)
    model.compile(optimizer="adam", loss="mse")

    # Load the dataset from the TFRecord file
    def parse_example(example_proto):
        feature_description = {
            'input': tf.io.FixedLenFeature([timesteps * n_mels], tf.float32),
            'target': tf.io.FixedLenFeature([n_mels * timesteps], tf.float32)
        }
        parsed_example = tf.io.parse_single_example(example_proto, feature_description)
        input_data = tf.reshape(parsed_example['input'], (timesteps, n_mels))
        target_data = tf.reshape(parsed_example['target'], (timesteps, n_mels))
        return input_data, target_data

    dataset = tf.data.TFRecordDataset(tfrecord_file).map(parse_example).shuffle(buffer_size=10000)

    # Calculate the number of batches for the validation split
    num_val_samples = int(validation_split * 10000)

    # Split the dataset into training and validation sets
    val_dataset = dataset.take(num_val_samples).batch(batch_size).repeat()
    train_dataset = dataset.skip(num_val_samples).batch(batch_size).repeat()

    # Train the model
    model.fit(train_dataset, epochs=epochs, steps_per_epoch=100, validation_data=val_dataset, validation_steps=25)

# Save the model
print("Saving the model...")
if use_tpu and not read_local:
    model_directory = os.path.join(gcs_bucket, model_directory)

os.makedirs(model_directory, exist_ok=True)
model.save(model_directory)
