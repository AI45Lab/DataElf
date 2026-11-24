"""Isolated query process entry point; no Pi or notebook-main dependency."""
import json
import sys
from .query import worker

class Output:
    def put(self,result):
        print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    args=json.load(sys.stdin)
    worker(Output(),args['quads'],args['text'],args['operation'],args['offset'],args['limit'],args.get('snapshot_path'),args.get('snapshot_metadata'))
