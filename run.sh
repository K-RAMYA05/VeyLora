#!/bin/bash
python3 -m venv new
source new/bin/activate
pip install -r requirements.txt
python app.py
