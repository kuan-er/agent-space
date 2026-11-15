"""Explicit local stdio bridge for ProductForm; no source or service auto-start."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from owned_source_product.controller import ProductController,PROTOCOL
from owned_source_product.records import read_json
from owned_source_product.submissions import ProductSubmissions,ExistingBindingGateway

LIMIT=262144
def serve(controller,input_stream,output_stream):
    try:
        while True:
            raw=input_stream.readline(LIMIT+1)
            if not raw:break
            if len(raw)>LIMIT or not raw.endswith(b'\n'):
                output_stream.write(b'{"ok":false,"reason":"bounded_command_required"}\n');output_stream.flush();break
            try:
                command=json.loads(raw.decode('utf-8'))
                result={'ok':True,'result':controller.handle(command)}
            except Exception as error:
                # Do not print goals, source records, credentials or stack locals.
                result={'ok':False,'reason':'operation_not_confirmed','error_type':type(error).__name__,
                        'instruction':'Keep the original action UUID; inspect its receipt. Do not resubmit.'}
            encoded=json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            if len(encoded)>LIMIT:encoded=b'{"ok":false,"reason":"response_budget"}'
            output_stream.write(encoded+b'\n');output_stream.flush()
            if controller.closed:break
    finally:
        # EOF closes the form only. It never cancels tasks or shuts down a host.
        controller.submissions.close_ui()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--node-root',required=True)
    parser.add_argument('--submissions-dir',required=True)
    parser.add_argument('--source-receipt',required=True)
    args=parser.parse_args()
    receipt=read_json(args.source_receipt)
    controller=ProductController(ProductSubmissions(args.submissions_dir,ExistingBindingGateway(args.node_root)),receipt)
    serve(controller,sys.stdin.buffer,sys.stdout.buffer)

if __name__=='__main__':main()
