import threading

from starrynet.sn_controller import *

### Warning: Never use this in public network!

from flask import Flask, request, Response, stream_with_context, jsonify

controller = TopoSync('./sn.json')

app = Flask(__name__)

@app.route('/execute', methods=['POST'])
def execute_command():
    data = request.json
    
    if 'command' not in data:
        return jsonify({'error': 'No command provided'}), 400
    if 'node' not in data:
        return jsonify({'error': 'Node not specified'}), 400    
    return Response(
        stream_with_context(controller.exec(data['node'], data['command'])),
        content_type='text/plain'
    )

api_thread = threading.Thread(
    target=app.run,
    args=('0.0.0.0', 5000)
)
api_thread.start()

controller.run()
