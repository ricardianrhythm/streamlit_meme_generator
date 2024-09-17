import streamlit as st
import requests
import json
import firebase_admin
from firebase_admin import credentials, firestore
import openai
from requests.exceptions import HTTPError
import traceback
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential
import os
import random

# Initialize Firebase (replace with your own credentials)
cred = credentials.Certificate(st.secrets["firebase"])
firebase_admin.initialize_app(cred)
db = firestore.client()

# Set up API keys using Streamlit secrets
IMGFLIP_USERNAME = st.secrets["IMGFLIP_USERNAME"]
IMGFLIP_PASSWORD = st.secrets["IMGFLIP_PASSWORD"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]

# Correct initialization of the OpenAI client
openai.api_key = OPENAI_API_KEY

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def call_openai_api(data):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {openai.api_key}'
    }
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, data=json.dumps(data))
        response.raise_for_status()
        return response.json()
    except HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
        return None
    except Exception as err:
        print(f"An error occurred: {err}")
        return None

def collect_user_ip():
    """
    Collect the user's IP address using Streamlit.
    """
    user_ip = st.experimental_get_query_params().get('user_ip', [None])[0]
    if not user_ip:
        user_ip = "Unknown IP"
    return user_ip

def get_meme_list():
    try:
        response = requests.get("https://api.imgflip.com/get_memes")
        response.raise_for_status()
        data = response.json()
        memes = data['data']['memes']
        return [{'name': meme['name'], 'id': meme['id'], 'box_count': meme['box_count']} for meme in memes[:100]]
    except requests.RequestException as e:
        print(f"Error fetching meme list: {e}")
        return []

# Streamlit app layout and logic
st.title("Big Red Button Meme Generator")

location = st.text_input("Enter your location:")
thought = st.text_input("Enter your thought:")
if st.button("Generate Meme"):
    ip_address = collect_user_ip()
    st.write(f"Collected IP: {ip_address}, Location: {location}, Thought: {thought}")

def generate_meme(thought, location_label, meme_id=None, previous_doc_id=None):
    try:
        print(f"Generating meme for thought: {thought} at location: {location_label}")
        meme_list = get_meme_list()
        if not meme_list:
            return None, None, None, "Error: Unable to fetch meme list"

        if meme_id:
            selected_meme = next((meme for meme in meme_list if meme['id'] == meme_id), None)
            if not selected_meme:
                return None, None, None, f"Meme with ID {meme_id} not found in meme list"
            box_count = selected_meme['box_count']
        else:
            meme_list_str = "\n".join(
                [f"{meme['name']} (ID: {meme['id']}, box_count: {meme['box_count']})" for meme in meme_list]
            )

            messages = [
                {"role": "system", "content": "You are an expert in meme creation. Your task is to select the most appropriate meme template based on a given thought and location, and generate witty and humorous text for the meme. Ensure that the meme is coherent and funny."},
                {"role": "user", "content": f"Here's a thought from {location_label}: {thought}\n\nHere is a list of available memes and their respective IDs and box counts:\n{meme_list_str}\n\nBased on this thought and location, which meme template would be the best fit?\nPlease provide:\nmeme: <name of meme>\nmeme_id: <id of meme>\nexplanation: <reason for the choice>"}
            ]

            data = {
                "model": "gpt-3.5-turbo",
                "temperature": .9,
                "messages": messages
            }

            response = call_openai_api(data)
            if response is None:
                return None, None, None, "Failed to get response from OpenAI API"

            meme_info = response['choices'][0]['message']['content']
            meme_dict = {line.split(": ")[0].strip(): line.split(": ")[1].strip() for line in meme_info.split("\n") if ": " in line}

            meme_id = meme_dict.get('meme_id')
            if not meme_id:
                return None, None, None, "Failed to retrieve meme_id from OpenAI response."

            selected_meme = next((meme for meme in meme_list if meme['id'] == meme_id), None)
            if not selected_meme:
                return None, None, None, f"Meme with ID {meme_id} not found in meme list"

            box_count = selected_meme['box_count']

        # Prompt for text boxes for the selected meme
        text_box_prompt = f"Great choice! Now, the selected meme requires {box_count} text boxes (from text0 to text{box_count - 1}). Please provide the text for each text box, ensuring that the combined texts create a coherent and humorous meme that relates to the thought and location:\n"
        for i in range(box_count):
            text_box_prompt += f"text{i}: <text for text box {i}>\n"

        messages.append({"role": "assistant", "content": meme_info})
        messages.append({"role": "user", "content": text_box_prompt})

        data['messages'] = messages

        response = call_openai_api(data)
        if response is None:
            return None, None, None, "Failed to get response from OpenAI API"

        text_boxes_info = response['choices'][0]['message']['content']
        text_boxes = {line.split(": ")[0].strip(): line.split(": ")[1].strip() for line in text_boxes_info.split("\n") if ": " in line}

        # Prepare parameters for Imgflip API
        url = "https://api.imgflip.com/caption_image"
        params = {
            "template_id": meme_id,
            "username": IMGFLIP_USERNAME,
            "password": IMGFLIP_PASSWORD,
        }

        if box_count > 2:
            for i in range(box_count):
                text_key = f"text{i}"
                text_value = text_boxes.get(text_key, '')
                params[f'boxes[{i}][text]'] = text_value
        else:
            params['text0'] = text_boxes.get('text0', '')
            params['text1'] = text_boxes.get('text1', '')

        response = requests.post(url, data=params)
        result = response.json()

        if result['success']:
            meme_url = result['data']['url']
            print(f"Meme URL generated successfully: {meme_url}")

            # Find the location document reference by label using the `where` method
            location_query = db.collection('locations').where('label', '==', location_label).limit(1).get()
            location_id = location_query[0].id if location_query else None
            print(f"Location ID found: {location_id}")

            # Save meme to Firebase, including the location ID
            try:
                print(f"Attempting to store meme in Firestore with thought: {thought}, location: {location_label}, location_id: {location_id}, meme_url: {meme_url}")
                
                # Correctly unpack the tuple returned by the add method
                write_time, doc_ref = db.collection('memes').add({
                    'thought': thought,
                    'location': location_label,
                    'location_id': location_id,
                    'meme_url': meme_url,
                    'explanation': meme_dict.get('explanation', ''),
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                # Now, doc_ref is the DocumentReference object.
                print(f"Meme stored in Firestore successfully. Document ID: {doc_ref.id}")
                return meme_url, meme_id, doc_ref.id, None
            except Exception as e:
                error_msg = f"Error storing meme in Firebase: {str(e)}"
                print(error_msg)
                traceback.print_exc()
                logging.error(f"Detailed error: {traceback.format_exc()}")
                return None, None, None, error_msg
        else:
            error_msg = f"Failed to generate meme. {result.get('error_message', '')}"
            print(error_msg)
            return None, None, None, error_msg

    except Exception as e:
        error_msg = f"Error in generate_meme: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        logging.error(f"Detailed error: {traceback.format_exc()}")
        return None, None, None, error_msg
                
def get_memes_from_firebase():
    try:
        memes = db.collection('memes').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(20).get()
        # Return a list of [image_url, caption]
        return [[meme.to_dict()['meme_url'], f"{meme.to_dict()['thought']} (Location: {meme.to_dict().get('location', '')})"] for meme in memes]
    except Exception as e:
        print(f"Error fetching memes from Firebase: {str(e)}")
        traceback.print_exc()
        return []
        

def get_locations_from_firebase():
    try:
        locations = db.collection('locations').get()
        print(f"Fetched {len(locations)} locations from Firebase.")
        location_labels = []
        for location in locations:
            data = location.to_dict()
            print(f"Document data: {data}")
            label = data.get('label', 'Unknown Location')
            print(f"Extracted label: {label}")
            location_labels.append(label)
        
        # Randomize the location options
        random.shuffle(location_labels)
        
        if not location_labels:
            location_labels = ["Other (specify below)"]
        else:
            location_labels.append("Other (specify below)")
        
        return location_labels
    except Exception as e:
        print(f"Error fetching locations from Firebase: {str(e)}")
        traceback.print_exc()
        return ["Other (specify below)"]  # Default option if fetching fails

def create_meme(selected_location, custom_location, thought, state_meme_id, state_thought, state_location_label, state_doc_id):
    if not thought.strip():
        return "Please enter your thought in the input field.", None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id

    used_thought = thought.strip()
    state_thought = used_thought  # Update state

    if selected_location == "Other (specify below)":
        if not custom_location.strip():
            return "Please enter a custom location.", None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
        used_label = custom_location.strip()
        # Add the custom location to Firebase
        try:
            location_ref = db.collection('locations').add({
                'label': used_label,
                'ip_address': ""  # Initialize IP address as an empty string
            })
            print(f"Added new location to Firebase: {used_label}")
        except Exception as e:
            print(f"Error adding new location to Firebase: {str(e)}")
            traceback.print_exc()
    else:
        used_label = selected_location
    state_location_label = used_label  # Update state

    # Collect IP address before generating the meme
    ip_address = collect_user_ip(state_location_label, state_thought)
    print(f"Collected IP Address: {ip_address}")  # Add debug print here

    # Ensure IP is collected and passed correctly
    if ip_address:
        print(f"Proceeding to update Firestore with IP: {ip_address} for location: {state_location_label}")
    
        # **Add the following code to generate the meme**
        meme_url, meme_id, doc_id, error = generate_meme(state_thought, state_location_label)
        if error:
            return error, None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
        else:
            state_meme_id = meme_id  # Update state with new meme_id
            state_doc_id = doc_id    # Update state with new document ID
            
            # Store the IP address as a string in the locations collection
            try:
                location_query = db.collection('locations').where('label', '==', state_location_label).limit(1).get()
                if location_query:
                    location_doc_id = location_query[0].id
                    print(f"Attempting to update location {state_location_label} with IP address {ip_address}.")
                    db.collection('locations').document(location_doc_id).update({
                        'ip_address': ip_address  # Store IP address as a string
                    })
                    print(f"IP address '{ip_address}' added to location: {state_location_label}")
                else:
                    print(f"Location {state_location_label} not found in the database.")
            except Exception as e:
                print(f"Error updating location with IP address: {str(e)}")
    
            # Store the meme details, including the IP address, in Firebase
            try:
                print(f"Storing meme details in Firestore with IP address: {ip_address}.")
                db.collection('memes').add({
                    'thought': state_thought,
                    'location': state_location_label,
                    'meme_url': meme_url,
                    'ip_address': ip_address,  # Store IP address as a string here
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                print(f"Meme and IP address stored in Firebase successfully.")
            except Exception as e:
                print(f"Error storing meme in Firebase: {str(e)}")
    else:
        print("IP address was not collected correctly.")

    # **Add the following code to generate the meme**
    meme_url, meme_id, doc_id, error = generate_meme(state_thought, state_location_label)
    if error:
        return error, None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
    else:
        state_meme_id = meme_id  # Update state with new meme_id
        state_doc_id = doc_id    # Update state with new document ID
        
        # Store the IP address as a string in the locations collection
        try:
            location_query = db.collection('locations').where('label', '==', state_location_label).limit(1).get()
            if location_query:
                location_doc_id = location_query[0].id
                print(f"Attempting to update location {state_location_label} with IP address {ip_address}.")
                db.collection('locations').document(location_doc_id).update({
                    'ip_address': ip_address  # Store IP address as a string
                })
                print(f"IP address '{ip_address}' added to location: {state_location_label}")
            else:
                print(f"Location {state_location_label} not found in the database.")
        except Exception as e:
            print(f"Error updating location with IP address: {str(e)}")

        # Store the meme details, including the IP address, in Firebase
        try:
            print(f"Storing meme details in Firestore with IP address: {ip_address}.")
            db.collection('memes').add({
                'thought': state_thought,
                'location': state_location_label,
                'meme_url': meme_url,
                'ip_address': ip_address,  # Store IP address as a string here
                'timestamp': firestore.SERVER_TIMESTAMP
            })
            print(f"Meme and IP address stored in Firebase successfully.")
        except Exception as e:
            print(f"Error storing meme in Firebase: {str(e)}")

    meme_html = f"""
    <div style='text-align: center;'>
        <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
        <p style='font-size: 1.2em; font-weight: bold;'>{state_thought}</p>
        <p style='font-size: 1em;'>Location: {state_location_label}</p>
    </div>
    """
    return "Meme generated successfully.", meme_html, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
    
def keep_template_change_words(state_meme_id, state_thought, state_location_label, state_doc_id):
    # Generate a new meme using the same template (meme_id) but potentially with different words
    meme_url, meme_id, doc_id, error = generate_meme(state_thought, state_location_label, meme_id=state_meme_id, previous_doc_id=state_doc_id)
    if error:
        return error, None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
    else:
        state_doc_id = doc_id  # Update state with new document ID

        meme_html = f"""
        <div style='text-align: center;'>
            <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
            <p style='font-size: 1.2em; font-weight: bold;'>{state_thought}</p>
            <p style='font-size: 1em;'>Location: {state_location_label}</p>
        </div>
        """
        return "Meme updated with new words.", meme_html, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id

def change_template(state_meme_id, state_thought, state_location_label, state_doc_id):
    # Generate a new meme, possibly changing both the template and the text
    meme_url, meme_id, doc_id, error = generate_meme(state_thought, state_location_label, previous_doc_id=state_doc_id)
    if error:
        return error, None, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id
    else:
        state_meme_id = meme_id  # Update state with new meme_id
        state_doc_id = doc_id    # Update state with new document ID

        meme_html = f"""
        <div style='text-align: center;'>
            <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
            <p style='font-size: 1.2em; font-weight: bold;'>{state_thought}</p>
            <p style='font-size: 1em;'>Location: {state_location_label}</p>
        </div>
        """
        return "Meme updated with a new template.", meme_html, get_memes_from_firebase(), state_meme_id, state_thought, state_location_label, state_doc_id

# Gradio interface
with gr.Blocks() as demo:
    gr.Markdown("# Big Red Button Meme Generator")

    # Fetch location labels from Firebase
    location_labels = get_locations_from_firebase()
    initial_location = random.choice(location_labels)  # Choose a random initial value

    with gr.Row():
        location_dropdown = gr.Dropdown(choices=location_labels, label="Select Location", value=initial_location)
        custom_location_input = gr.Textbox(label="Enter custom location", visible=False)
    
    thought_input = gr.Textbox(label="Enter your thought")
    submit_btn = gr.Button("Generate Meme")
    clear_btn = gr.Button("Clear", visible=True)  # New "Clear" button

    status_output = gr.Textbox(label="Status")
    meme_output = gr.HTML(label="Generated Meme")
    
    # Add buttons for user options (initially hidden)
    with gr.Row():
        btn_keep_template_change_words = gr.Button("Keep the meme, change the words", visible=False)
        btn_change_template = gr.Button("Try different meme", visible=False)
        btn_create_new_meme = gr.Button("Prompt updated, Try Again", visible=False)
    
    meme_gallery = gr.Gallery(
        label="Previous Memes", 
        show_label=True,
        columns=1,
        height="auto",
        object_fit="contain"
    )
    
    # Define state variables
    state_meme_id = gr.State()
    state_thought = gr.State()
    state_location_label = gr.State()
    state_doc_id = gr.State()  # New state variable for document ID
    
    def update_custom_location(selected_location):
        if selected_location == "Other (specify below)":
            return gr.update(visible=True)
        else:
            return gr.update(visible=False)
    
    location_dropdown.change(
        update_custom_location,
        inputs=location_dropdown,
        outputs=custom_location_input
    )
    
    def clear_inputs():
        return "", "", gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    clear_btn.click(
        clear_inputs,
        inputs=[],
        outputs=[thought_input, status_output, submit_btn, btn_keep_template_change_words, btn_change_template, btn_create_new_meme]
    )

    def generate_meme_action(*args):
        result = create_meme(*args)
        # Hide "Generate Meme" button and show other buttons after generating meme
        return (*result, gr.update(visible=False), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True))

    submit_btn.click(
        generate_meme_action,
        inputs=[location_dropdown, custom_location_input, thought_input, state_meme_id, state_thought, state_location_label, state_doc_id],
        outputs=[status_output, meme_output, meme_gallery, state_meme_id, state_thought, state_location_label, state_doc_id, submit_btn, btn_keep_template_change_words, btn_change_template, btn_create_new_meme]
    )
    
    btn_keep_template_change_words.click(
        keep_template_change_words,
        inputs=[state_meme_id, state_thought, state_location_label, state_doc_id],
        outputs=[status_output, meme_output, meme_gallery, state_meme_id, state_thought, state_location_label, state_doc_id]
    )
    
    btn_change_template.click(
        change_template,
        inputs=[state_meme_id, state_thought, state_location_label, state_doc_id],
        outputs=[status_output, meme_output, meme_gallery, state_meme_id, state_thought, state_location_label, state_doc_id]
    )
    
    demo.load(get_memes_from_firebase, outputs=meme_gallery)

if __name__ == "__main__":
    demo.launch(share=True)
