import os
import time
import json
import shutil
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import google.generativeai as genai
import pandas as pd
from PyPDF2 import PdfReader, PdfWriter
from datetime import datetime
from dateutil.parser import parse
from difflib import SequenceMatcher
from PIL import Image
from pydantic import BaseModel
from typing import List, Dict, Optional
import tempfile
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# Configure API key (use environment variables for safety)
private_key_royal = "AIzaSyAVTgGiNx8iKvZOBgbY9zho7drkHd5vQeI"  # S.Rohith Royal's
private_key_siri = "AIzaSyAYe-nb8p3B-xSUKrZOLz9yOd8ukMZ8AcI"  # Siresharajanala's
api_key = os.getenv("GENAI_API_KEY", private_key_siri)
genai.configure(api_key=api_key)

EXCEL_FILE_PATH = "invoice_data.xlsx"
REQUIRED_COLUMNS = [
    'Invoice Number', 'Invoice Date', 'Total Invoice Amount',
    'Total Invoice Amount (In Words)', 'GST Amount', 'Taxable Amount',
    'CGST', 'SGST', 'IGST', 'CESS', 'GST Percentage',
    'Vendor Name', 'Vendor Mobile', 'Vendor GSTIN', 'Vendor PAN',
    'Vendor Address', 'Vendor City', 'Vendor State',
    'Client Name', 'Client Mobile', 'Client GSTIN', 'Client PAN',
    'Client Address','Client City', 'Client State', 'Signature Present'
]

if not os.path.exists(EXCEL_FILE_PATH):
    pd.DataFrame(columns=REQUIRED_COLUMNS).to_excel(EXCEL_FILE_PATH, index=False)

def upload_to_gemini(path, mime_type="application/pdf"):
    """Uploads the given file to Gemini."""
    try:
        file = genai.upload_file(path, mime_type=mime_type)
        print(f"Uploaded file '{file.display_name}' as: {file.uri}")
        return file
    except Exception as e:
        print(f"Failed to upload file: {e}")
        return None

def wait_for_files_active(files):
    """Waits for the given files to be active."""
    print("Waiting for file processing...")
    for name in (file.name for file in files):
        while True:
            try:
                file = genai.get_file(name)
                if file.state.name == "ACTIVE":
                    break
                elif file.state.name != "PROCESSING":
                    raise Exception(f"File {file.name} failed with state: {file.state.name}")
                print(".", end="", flush=True)
                time.sleep(5)  # Reduced sleep time to make it more responsive
            except Exception as e:
                print(f"Error checking file status: {e}")
                break
    print("\n...all files ready")

def update_excel(data: dict):
    """Update Excel file with new extracted data"""
    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
       
        new_row = {
            'Invoice Number': data.get('invoiceNumber', 'N/A'),
            'Invoice Date': data.get('invoiceDate', 'N/A'),
            'Total Invoice Amount': data.get('totalInvoiceAmount', 'N/A'),
            'Total Invoice Amount (In Words)': data.get('totalInvoiceAmountInWords', 'N/A'),
            'GST Amount': data.get('gstAmount', 'N/A'),
            'Taxable Amount': data.get('taxableAmount', 'N/A'),
            'CGST': data.get('gstBreakdown', {}).get('cgst', 'N/A'),
            'SGST': data.get('gstBreakdown', {}).get('sgst', 'N/A'),
            'IGST': data.get('gstBreakdown', {}).get('igst', 'N/A'),
            'CESS': data.get('gstBreakdown', {}).get('cess', 'N/A'),
            'GST Percentage': data.get('gstBreakdown', {}).get('gstPercentage', 'N/A'),
            'Vendor Name': data.get('vendorDetails', {}).get('name', 'N/A'),
            'Vendor Mobile': data.get('vendorDetails', {}).get('mobileNumber', 'N/A'),
            'Vendor GSTIN': data.get('vendorDetails', {}).get('gstin', 'N/A'),
            'Vendor PAN': data.get('vendorDetails', {}).get('pan', 'N/A'),
            'Vendor Address': data.get('vendorDetails', {}).get('address', 'N/A'),
            'Vendor City': data.get('vendorDetails', {}).get('city', 'N/A'),
            'Vendor State': data.get('vendorDetails', {}).get('state', 'N/A'),
            'Client Name': data.get('clientDetails', {}).get('name', 'N/A'),
            'Client Mobile': data.get('clientDetails', {}).get('mobileNumber', 'N/A'),
            'Client GSTIN': data.get('clientDetails', {}).get('gstin', 'N/A'),
            'Client PAN': data.get('clientDetails', {}).get('pan', 'N/A'),
            'Client Address': data.get('clientDetails', {}).get('address', 'N/A'),
            'Client City': data.get('clientDetails', {}).get('city', 'N/A'),
            'Client State': data.get('clientDetails', {}).get('state', 'N/A'),
            'Signature Present': data.get('signaturePresent', 'No')
        }
       
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE_PATH, index=False)
        return True
    except Exception as e:
        print(f"Error updating Excel: {e}")
        return False

def process_invoice(model, chunk_file_path):
    """Process a single invoice PDF file and return extracted data."""
    print("Processing Invoice PDF...")
    try:
        chat_session = model.start_chat(
            history=[
                {
                    "role": "user",
                    "parts": [
                        chunk_file_path,
                        '''Analyze the uploaded invoice PDF containing detailed information. Identify and extract the following fields:
   
                        If a fieldâ€™s value is unclear (e.g., partially visible or handwritten), retain the raw text (if available) and infer the field context where possible. For fields not found, return "N/A".
   
                        - Invoice Number (if not found, check for numbers beside "No." or numbers in red color)
                        - Invoice Date
                        - Total Invoice Amount
                        - Total Invoice Amount (In Words)
                        - GST Amount
                        - Taxable Amount
                        - CGST, SGST, IGST, CESS (with percentages and amounts)
                        - Vendor Name
                        - Vendor Mobile Number
                        - Vendor GSTIN
                        - Vendor PAN
                        - Vendor Address (Full, City, State)
                        - Client Name
                        - Client Mobile Number
                        - Client Address (City, State)
                        - Client GSTIN
                        - Client PAN
                        - Signature Present
   
                        Extract and structure the data in the following JSON format:
   
                        {
                          "invoiceData": {
                            "invoiceNumber": "",
                            "invoiceDate": "",
                            "totalInvoiceAmount": "",
                            "totalInvoiceAmountInWords": "",
                            "gstAmount": "",
                            "taxableAmount": "",
                            "gstBreakdown": {
                              "cgst": "",
                              "sgst": "",
                              "igst": "",
                              "cess": "",
                              "gstPercentage": ""
                            },
                            "vendorDetails": {
                              "name": "",
                              "mobileNumber": "",
                              "gstin": "",
                              "pan": "",
                              "address": "",
                              "city": "",
                              "state": ""
                            },
                            "clientDetails": {
                              "name": "",
                              "mobileNumber": "",
                              "gstin": "",
                              "pan": "",
                              "address": "",
                              "city": "",
                              "state": ""
                            },
                            "signaturePresent": ""
                          }
                        }
   
                        '''
                    ],
                },
            ]
        )
        response = chat_session.send_message("Extract the data as requested.")
        return parse_json_response(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing error: {str(e)}")

def parse_json_response(response_text):
    """Attempt to parse JSON from the response text."""
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        try:
            start = response_text.find('{')
            end = response_text.rfind('}') + 1
            if start != -1 and end != -1:
                json_str = response_text[start:end]
                return json.loads(json_str)
            else:
                raise ValueError("No valid JSON object found in the response")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error parsing JSON: {e}")
            print("Response content:")
            print(response_text)
            return {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins (or specify frontend URL)
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)

def convert_tiff_to_pdf(input_file, output_file):
    """
    Convert a multi-page TIFF file to a single PDF.
   
    Args:
        input_file (str): The path to the input TIFF file.
        output_file (str): The path to save the output PDF file.
    """
    try:
        # Open the input TIFF file
     
        with Image.open(input_file) as img:
            # List to hold the individual pages of the TIFF
           
            image_pages = []
     
            # Process each frame (page) in the multi-page TIFF
            for page in range(img.n_frames):
                img.seek(page)  # Go to the specified page
               
                # Convert the page to RGB if necessary
                if img.mode in ('RGBA', 'P'):
                    img_page = img.convert('RGB')
                else:
                    img_page = img.copy()

                # Add the processed page to the list of image pages
                image_pages.append(img_page)
           
            # Save all pages as a single PDF
            if image_pages:
           
                image_pages[0].save(output_file, save_all=True, append_images=image_pages[1:], resolution=100.0)
                print(f"Successfully converted {input_file} to {output_file}")
            else:
                print(f"No pages found in {input_file}.")
               
    except Exception as e:
        print(f"Error converting {input_file} to PDF: {e}")

def batch_convert_to_pdf(input_files, document_type, output_dir="converted_pdfs"):
    """
    Convert a list of image files (including multi-page TIFFs) to PDFs
    and save them in the specified output directory.

    Args:
        input_files (list): List of file paths to be converted.
        document_type (str): Type of document (used for metadata or logging).
        output_dir (str): Directory where PDFs will be saved. Defaults to 'converted_pdfs'.

    Returns:
        list: A list of output file paths for the converted PDFs.
    """
    # Ensure the output directory exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    output_file=""

    # Iterate through each file in the input list
    for input_file in input_files:
        try:
         
            # Extract the file name without extension
            file_name = os.path.splitext(os.path.basename(input_file))[0]  # Correct way to get the file name without extension
           
            # Define the output PDF file path
            output_file = os.path.join(output_dir, f"{file_name}.pdf")
            #print(input_file,output_file)
            # Call the function to convert TIFF to PDF
            #json_output = convert_tiff_to_pdf(input_file, output_file, document_type)
           
            json_output = convert_tiff_to_pdf(input_file, output_file)
            # Append the output file path to the list
         

            print(f"Converted {input_file} tooo {output_file}")

        except Exception as e:
            print(f"Error processing file {input_file}: {e}")
 
    # Return the list of output files
    #print("test")
    #print(output_file)
    return output_file

# Pydantic models for request/response
class ProcessRequest(BaseModel):
    date_format: str = "%d/%m/%Y"

class ProcessResponse(BaseModel):
    extracted_data: List[Dict]

# Configuration setup
def get_generation_config():
    return {
        "temperature": 1,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 8192,
        "response_mime_type": "text/plain",
    }

def initialize_model():
    generation_config = get_generation_config()   # gemini-1.5-flash-002
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash-002",
        generation_config=generation_config,
        safety_settings="BLOCK_NONE",
        system_instruction=(
            "You are in charge of extracting the data from Invoice PDF documents. "
            "Follow the prompt for required fields. The output should be in a JSON format. "
            "If you are not able to find the value, reply with N/A."
        ),
    )

@app.post("/Process_Invoices/")
async def ProcessInvoices(
    # date_format: str = Form(...),
    files: list[UploadFile] = File(
        ...,
        description="Multiple files to be processed"
    )
):
    try:
        # Create request object
        # request = ProcessRequest(
        #     date_format=date_format
        # )

        # Initialize model
        print("Starting processing...")
        model = initialize_model()

        # Process uploaded files
        all_extracted_data = []
        for file in files:
            # Save uploaded file temporarily
            temp_path = f"temp_{file.filename}"
            with open(temp_path, "wb") as buffer:
                content = await file.read()
                buffer.write(content)

            # Convert to PDF if needed
            if file.filename.lower().endswith('.tif'):
                pdf_path = batch_convert_to_pdf([temp_path], ["invoice"])
            else:
                pdf_path = temp_path

            # Upload to Gemini
            gemini_file = upload_to_gemini(pdf_path, mime_type="application/pdf")
            if gemini_file is None:
                continue

            wait_for_files_active([gemini_file])

            # Extract invoice data
            invoice_data = process_invoice(model, gemini_file)
            all_extracted_data.append(invoice_data)

            update_excel(invoice_data.get('invoiceData', {}))
            #     return {"message": "Data extracted and saved successfully!"}
            # else:
            #     raise HTTPException(status_code=500, detail="Failed to update Excel file")

            # Cleanup temporary files
            try:
                os.remove(temp_path)
            except Exception as e:
                print(f"Error removing temp file {temp_path}: {e}")

        return JSONResponse(
            status_code=200,
            content={
                "message": f"Successfully processed {len(all_extracted_data)} files!",
                "data": all_extracted_data
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download_excel/")
async def download_excel():
    if not os.path.exists(EXCEL_FILE_PATH):
        raise HTTPException(status_code=404, detail="Excel file not found")
   
    return FileResponse(
        EXCEL_FILE_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="invoice_data.xlsx"
    )

# Health check endpoint
# @app.get("/health")
# async def health_check():
#     return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8002)