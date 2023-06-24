#!/bin/sh
​
set -eux
​
ENV="env_linkpred"
PYTHON_V="3.10"​
​
# create conda environment
eval "$($CONDA_DIR/bin/conda shell.bash hook)"
conda create -y -n $ENV python=$PYTHON_V
​
# activate
conda activate $ENV
​
# install pytorch
pip3 install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
​
# install some extensions
pip install pyg_lib torch_scatter torch_sparse torch_cluster torch_spline_conv -f https://data.pyg.org/whl/torch-2.0.0+cu118.html
​
# install torchdrug
cd ..
git clone https://github.com/DeepGraphLearning/torchdrug
cd torchdrug
pip install -r requirements.txt
python setup.py install
cd ../NBFNet
​
# install some auxiliary libraries
pip install ogb easydict pyyaml
