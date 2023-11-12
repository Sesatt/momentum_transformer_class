import keras_tuner as kt

# HP_HIDDEN_LAYER_SIZE = [5, 10, 20, 40, 80, 160]
HP_HIDDEN_LAYER_SIZE = [80]
# HP_DROPOUT_RATE = [0.1, 0.2, 0.3, 0.4, 0.5]
HP_DROPOUT_RATE = [0.1, 0.3, 0.5]
HP_MINIBATCH_SIZE= [64, 128, 256, 512]
# HP_MINIBATCH_SIZE= [32, 64, 128]
HP_LEARNING_RATE = [1e-4, 1e-3, 1e-2, 1e-1]
HP_MAX_GRADIENT_NORM = [0.01, 1.0, 100.0]

# RANDOM_SEARCH_ALGORITHM = kt.RandomSearch
