"""Manual browser smoke test: synthetic model, isolated data, no external API calls."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grader.main import create_app
from test_model_api import FakeModel
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(args.data, model_factory=FakeModel), host='127.0.0.1', port=8002, access_log=False)
