from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
from dotenv import load_dotenv
from ragie import Ragie
from openai import OpenAI
from flasgger import Swagger
from datetime import datetime
from collections import deque
import threading
import csv
import io
import json

load_dotenv()

# In-memory store for interactions (for production, use a database)
# Using deque with maxlen to limit memory usage (keeps last 1000 interactions)
interactions_log = deque(maxlen=1000)
log_lock = threading.Lock()

app = Flask(__name__)
# Configure CORS to allow all origins and methods - more permissive for Render
# This handles preflight OPTIONS requests automatically
CORS(app, 
     resources={r"/*": {
         "origins": "*", 
         "methods": ["GET", "POST", "OPTIONS", "HEAD"],
         "allow_headers": ["Content-Type", "Authorization", "Accept"],
         "expose_headers": ["Content-Type"],
         "supports_credentials": False
     }})

# Swagger UI - using relative URLs so it works on any host
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": "apispec",
            "route": "/apispec.json",
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/api-docs"
}

# Swagger template - no host specified, Swagger UI will use current origin
swagger_template = {
    "swagger": "2.0",
    "info": {
        "title": "The Side Letter Chat API",
        "description": "API for querying The Side Letter knowledge base using Ragie and ChatGPT",
        "version": "1.0.0"
    },
    # No host specified - Swagger UI will use the current page's origin
    "basePath": "/",
    "schemes": ["https", "http"]
}

swagger = Swagger(app, config=swagger_config, template=swagger_template)

# Override the Swagger spec endpoint to inject correct host
@app.route('/apispec.json')
def custom_swagger_spec():
    """Return Swagger spec with current request host"""
    from flask import jsonify, request
    # Get the spec from Swagger
    spec = swagger.get_apispecs()
    # Update with current host
    if isinstance(spec, dict):
        spec['host'] = request.host
        # Prefer HTTPS for Render deployments
        if 'onrender.com' in request.host or os.environ.get('RENDER'):
            spec['schemes'] = ['https', 'http']
        else:
            spec['schemes'] = ['http', 'https']
    return jsonify(spec)

# Add CORS headers to all responses (backup, though flask-cors should handle this)
@app.after_request
def after_request(response):
    # Only add if not already set by flask-cors
    if 'Access-Control-Allow-Origin' not in response.headers:
        response.headers.add('Access-Control-Allow-Origin', '*')
    if 'Access-Control-Allow-Headers' not in response.headers:
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept')
    if 'Access-Control-Allow-Methods' not in response.headers:
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, HEAD')
    # Add max-age for preflight caching
    if 'Access-Control-Max-Age' not in response.headers:
        response.headers.add('Access-Control-Max-Age', '3600')
    return response

# Initialize clients lazily to avoid startup errors
ragie = None
openai_client = None

def get_ragie_client():
    """Get or create Ragie client"""
    global ragie
    if ragie is None:
        api_key = os.environ.get("RAGIE_API_KEY")
        if not api_key:
            raise ValueError("RAGIE_API_KEY environment variable is not set")
        ragie = Ragie(auth=api_key)
    return ragie

def get_openai_client():
    """Get or create OpenAI client"""
    global openai_client
    if openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set")
        openai_client = OpenAI(api_key=api_key)
    return openai_client

def get_system_prompt():
    """Load system prompt from external file"""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        prompt_path = os.path.join(base_dir, 'system_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error loading system prompt: {e}")
        # Fallback basic prompt
        return "You are Side Letter’s research partner for allocators."

SYSTEM_PROMPT = get_system_prompt()

@app.route('/api/chat', methods=['POST', 'OPTIONS'])
def chat():
    """
    Chat endpoint - Query The Side Letter knowledge base
    ---
    tags:
      - Chat
    summary: Ask a question about The Side Letter content
    description: |
      This endpoint allows you to ask questions about The Side Letter's knowledge base.
      It queries Ragie for relevant document chunks, then uses ChatGPT to synthesize
      a comprehensive answer based on the retrieved context.
    consumes:
      - application/json
    produces:
      - application/json
    parameters:
      - in: body
        name: body
        description: Question to ask
        required: true
        schema:
          type: object
          required:
            - question
          properties:
            question:
              type: string
              example: "Who are interesting funds investing in defense tech?"
              description: The question to ask about The Side Letter content
    responses:
      200:
        description: Successful response with answer and sources
        schema:
          type: object
          properties:
            answer:
              type: string
              example: "Based on the provided context..."
            sources:
              type: array
              items:
                type: object
                properties:
                  id:
                    type: string
                  name:
                    type: string
                  score:
                    type: number
                  snippet:
                    type: string
            question:
              type: string
            success:
              type: boolean
      400:
        description: Bad request - question is required
      500:
        description: Internal server error
    """
    # Handle CORS preflight explicitly (though flask-cors should handle this)
    if request.method == 'OPTIONS':
        response = jsonify({})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, Accept')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS, HEAD')
        response.headers.add('Access-Control-Max-Age', '3600')
        return response, 200
    
    try:
        # Get clients (will raise error if keys missing)
        ragie_client = get_ragie_client()
        openai_client_instance = get_openai_client()
        
        data = request.json
        if not data:
            return jsonify({'error': 'Request body is required', 'success': False}), 400
        
        user_question = data.get('question', '').strip()
        
        if not user_question:
            return jsonify({'error': 'Question is required', 'success': False}), 400
        
        print(f"Received question: {user_question}")
        
        print(f"  Querying Ragie...")
        retrieval_response = ragie_client.retrievals.retrieve(
            request={
                "query": user_question,
                "top_k": 15,
                "rerank": True 
            }
        )
        
        context_chunks = []
        sources = []
        
        # both dict and object responses from Ragie
        scored_chunks = retrieval_response.get('scored_chunks', []) if isinstance(retrieval_response, dict) else retrieval_response.scored_chunks
        
        for idx, chunk in enumerate(scored_chunks):
            chunk_text = chunk.get('text') if isinstance(chunk, dict) else chunk.text
            chunk_score = chunk.get('score', 0) if isinstance(chunk, dict) else chunk.score
            
            context_chunks.append(chunk_text)
            
            if isinstance(chunk, dict):
                doc_metadata = chunk.get('document_metadata', {})
                doc_id = doc_metadata.get('id', 'unknown')
                doc_name = doc_metadata.get('name', 'Unknown Document')
            else:
                doc_id = getattr(chunk.document_metadata, 'id', 'unknown')
                doc_name = getattr(chunk.document_metadata, 'name', 'Unknown Document')
            
            source_info = {
                'id': doc_id,
                'name': doc_name,
                'score': round(chunk_score, 3),
                'snippet': chunk_text[:150] + '...' if len(chunk_text) > 150 else chunk_text
            }
            sources.append(source_info)
        
        print(f"  Found {len(context_chunks)} relevant chunks")
        
        if not context_chunks:
            response = jsonify({
                'answer': "We don’t have strong coverage here yet. Would you like us to flag this for updated or expanded coverage?",
                'sources': [],
                'question': user_question,
                'success': True
            })
            response.headers.add('Access-Control-Allow-Origin', '*')
            return response
        
        combined_context = "\n\n".join([
            f"[Source {i+1} - {sources[i]['name']}]:\n{chunk}" 
            for i, chunk in enumerate(context_chunks)
        ])
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"""Context from The Side Letter knowledge base:

{combined_context}

---

User Question: {user_question}

Please provide a comprehensive, detailed answer based on ALL relevant information in the context above. Format your response in a clear, readable way that best presents the information. Include all relevant details, specifics, and context from the sources."""}
        ]
        
        print("  Calling ChatGPT...")
        chat_completion = openai_client_instance.chat.completions.create(
            model="gpt-4o", 
            messages=messages,
            temperature=0.7,
            max_tokens=2000 
        )
        
        answer = chat_completion.choices[0].message.content
        print(f"  Generated answer ({len(answer)} chars)")
        
        # Log the interaction
        interaction = {
            'id': len(interactions_log) + 1,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'question': user_question,
            'answer': answer,
            'sources': sources,
            'sources_count': len(sources),
            'answer_length': len(answer)
        }
        
        with log_lock:
            interactions_log.append(interaction)
        
        print(f"  Logged interaction #{interaction['id']}")
        
        # 6. Return response with CORS headers
        # Return the formatted plain text answer (LLM handles all formatting)
        response = jsonify({
            'answer': answer,  # Formatted plain text from LLM
            'sources': sources,
            'question': user_question,
            'success': True,
            'interaction_id': interaction['id']  # Return the interaction ID
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response
        
    except ValueError as e:
        # Missing API keys
        print(f"  Configuration Error: {str(e)}")
        response = jsonify({
            'error': f'Configuration error: {str(e)}',
            'success': False
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 500
    except Exception as e:
        print(f"  Error: {str(e)}")
        import traceback
        traceback.print_exc()
        response = jsonify({
            'error': str(e),
            'success': False
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 500

@app.route('/', methods=['GET'])
def root():
    """Root endpoint"""
    return jsonify({
        'message': 'The Side Letter Chat API is running',
        'endpoints': {
            '/api/chat': 'POST - Chat endpoint',
            '/api/logs': 'GET - Get interaction logs',
            '/health': 'GET - Health check',
            '/api-docs': 'GET - API documentation',
            '/api/test': 'GET - CORS test endpoint'
        }
    })

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """
    Get interaction logs
    ---
    tags:
      - Logs
    summary: Get interaction logs
    description: Returns a list of recent question/answer interactions
    parameters:
      - in: query
        name: limit
        type: integer
        description: Maximum number of logs to return (default 50, max 200)
        required: false
      - in: query
        name: offset
        type: integer
        description: Number of logs to skip (for pagination)
        required: false
    responses:
      200:
        description: List of interactions
        schema:
          type: object
          properties:
            logs:
              type: array
              items:
                type: object
                properties:
                  id:
                    type: integer
                  timestamp:
                    type: string
                  question:
                    type: string
                  answer:
                    type: string
                  sources_count:
                    type: integer
                  answer_length:
                    type: integer
            total:
              type: integer
            limit:
              type: integer
            offset:
              type: integer
    """
    try:
        limit = min(int(request.args.get('limit', 50)), 200)  # Max 200
        offset = int(request.args.get('offset', 0))
        
        with log_lock:
            logs_list = list(interactions_log)
            total = len(logs_list)
            # Reverse to show most recent first
            logs_list.reverse()
            # Apply pagination
            paginated_logs = logs_list[offset:offset + limit]
            
            # Return full responses in list view
            logs_summary = []
            for log in paginated_logs:
                logs_summary.append({
                    'id': log['id'],
                    'timestamp': log['timestamp'],
                    'question': log['question'],
                    'answer': log['answer'],  # Full answer
                    'sources': log['sources'],  # Full sources
                    'sources_count': log['sources_count'],
                    'answer_length': log['answer_length']
                })
        
        return jsonify({
            'logs': logs_summary,
            'total': total,
            'limit': limit,
            'offset': offset
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs/<int:log_id>', methods=['GET'])
def get_log_detail(log_id):
    """
    Get detailed log by ID
    ---
    tags:
      - Logs
    summary: Get detailed interaction log
    description: Returns full details of a specific interaction including full answer and sources
    parameters:
      - in: path
        name: log_id
        type: integer
        required: true
        description: The interaction ID
    responses:
      200:
        description: Interaction details
      404:
        description: Log not found
    """
    try:
        with log_lock:
            for log in interactions_log:
                if log['id'] == log_id:
                    return jsonify(log)
        
        response = jsonify({'error': 'Log not found'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 404
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 500

@app.route('/api/logs/export', methods=['GET'])
def export_logs():
    """
    Export interaction logs to file
    ---
    tags:
      - Logs
    summary: Export logs to file
    description: Exports interaction logs in CSV, JSON, or TXT format
    parameters:
      - in: query
        name: format
        type: string
        enum: [csv, json, txt]
        description: Export format (default: json)
        required: false
      - in: query
        name: limit
        type: integer
        description: Maximum number of logs to export (default: all, max: 1000)
        required: false
    responses:
      200:
        description: File download
    """
    try:
        export_format = request.args.get('format', 'json').lower()
        limit = min(int(request.args.get('limit', 1000)), 1000)  # Max 1000
        
        with log_lock:
            logs_list = list(interactions_log)
            total = len(logs_list)
            # Reverse to show most recent first
            logs_list.reverse()
            # Limit if specified
            if limit < total:
                logs_list = logs_list[:limit]
        
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        
        if export_format == 'csv':
            # Export as CSV
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Write header
            writer.writerow(['ID', 'Timestamp', 'Question', 'Answer', 'Sources Count', 'Answer Length'])
            
            # Write data
            for log in logs_list:
                writer.writerow([
                    log['id'],
                    log['timestamp'],
                    log['question'],
                    log['answer'],
                    log['sources_count'],
                    log['answer_length']
                ])
            
            response = Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={
                    'Content-Disposition': f'attachment; filename=interactions_{timestamp}.csv',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            return response
            
        elif export_format == 'txt':
            # Export as TXT
            output = io.StringIO()
            output.write(f"Interaction Logs Export\n")
            output.write(f"Generated: {datetime.utcnow().isoformat()}Z\n")
            output.write(f"Total Interactions: {len(logs_list)}\n")
            output.write("=" * 80 + "\n\n")
            
            for log in logs_list:
                output.write(f"ID: {log['id']}\n")
                output.write(f"Timestamp: {log['timestamp']}\n")
                output.write(f"Question: {log['question']}\n")
                output.write(f"Answer:\n{log['answer']}\n")
                output.write(f"Sources Count: {log['sources_count']}\n")
                output.write(f"Answer Length: {log['answer_length']} chars\n")
                output.write("-" * 80 + "\n\n")
            
            response = Response(
                output.getvalue(),
                mimetype='text/plain',
                headers={
                    'Content-Disposition': f'attachment; filename=interactions_{timestamp}.txt',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            return response
            
        else:  # Default to JSON
            # Export as JSON
            export_data = {
                'export_timestamp': datetime.utcnow().isoformat() + 'Z',
                'total_interactions': len(logs_list),
                'logs': logs_list
            }
            
            response = Response(
                json.dumps(export_data, indent=2),
                mimetype='application/json',
                headers={
                    'Content-Disposition': f'attachment; filename=interactions_{timestamp}.json',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            return response
            
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response, 500

@app.route('/api/test', methods=['GET', 'OPTIONS'])
def test_cors():
    """Test endpoint to verify CORS is working"""
    if request.method == 'OPTIONS':
        response = jsonify({})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Methods', 'GET, OPTIONS')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        return response, 200
    
    return jsonify({
        'message': 'CORS test successful',
        'timestamp': __import__('datetime').datetime.now().isoformat(),
        'origin': request.headers.get('Origin', 'Not provided'),
        'cors_working': True
    })

@app.route('/health', methods=['GET'])
def health():
    """
    Health check endpoint
    ---
    tags:
      - Health
    summary: Check API health status
    description: Returns the health status of the API and connected services
    responses:
      200:
        description: API is healthy
        schema:
          type: object
          properties:
            status:
              type: string
              example: "healthy"
            ragie_connected:
              type: boolean
            openai_connected:
              type: boolean
    """
    return jsonify({
        'status': 'healthy',
        'ragie_connected': bool(os.environ.get("RAGIE_API_KEY")),
        'openai_connected': bool(os.environ.get("OPENAI_API_KEY"))
    })

@app.route('/api/documents', methods=['GET'])
def list_documents():
    """
    List documents endpoint
    ---
    tags:
      - Documents
    summary: Get information about available documents
    description: Returns information about documents in the Ragie knowledge base
    responses:
      200:
        description: Success
        schema:
          type: object
          properties:
            message:
              type: string
            success:
              type: boolean
    """
    try:
        # This endpoint can be used to show what documents are available
        return jsonify({
            'message': 'Documents are managed in Ragie dashboard',
            'success': True
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', debug=True, port=port)

