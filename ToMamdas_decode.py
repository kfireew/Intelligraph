#!/usr/bin/env python3
"""
ToMamdas Decoder - Hackathon Challenge Solution
Decodes Word documents with 1x1 tables back into the original tar file.
No external dependencies required - uses only Python standard library.
"""

import os
import sys
import base64
import hashlib
import glob
import re
import zipfile
import xml.etree.ElementTree as ET

# Configuration
INPUT_PREFIX = "chunk_"
INPUT_EXTENSION = ".docx"

def calculate_sha256(data):
    """Calculate SHA256 hash of data."""
    return hashlib.sha256(data).hexdigest()

def extract_chunk_number(filename):
    """Extract chunk number from filename like 'chunk_001.docx'."""
    match = re.search(r'chunk_(\d+)\.docx', filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def read_chunk_from_word(doc_path):
    """Read base64 chunk data from a Word document's 1x1 table using only standard library."""
    try:
        # .docx files are ZIP archives containing XML files
        with zipfile.ZipFile(doc_path, 'r') as docx_zip:
            # Read the main document XML
            xml_content = docx_zip.read('word/document.xml')
            
        # Parse XML
        root = ET.fromstring(xml_content)
        
        # Define namespace for Word documents
        namespaces = {
            'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        }
        
        # Extract all text from table cells (w:t elements within w:tbl)
        text_parts = []
        for text_elem in root.findall('.//w:tbl//w:t', namespaces):
            if text_elem.text:
                text_parts.append(text_elem.text)
        
        if not text_parts:
            print(f"WARNING: No text found in table in {os.path.basename(doc_path)}")
            return None
        
        # Join all text parts (base64 data)
        chunk_data = ''.join(text_parts)
        
        return chunk_data
    
    except Exception as e:
        print(f"ERROR reading {os.path.basename(doc_path)}: {str(e)}")
        return None

def decode_word_to_tar(input_dir, output_tar_path):
    """
    Main decoding function: reads Word documents, extracts chunks, reconstructs tar file.
    """
    print(f"ToMamdas Decoder - Starting decoding process...")
    print(f"Input directory: {input_dir}")
    
    # Check if input directory exists
    if not os.path.exists(input_dir):
        print(f"ERROR: Directory '{input_dir}' not found!")
        return False
    
    # Find all chunk files
    pattern = os.path.join(input_dir, f"{INPUT_PREFIX}*{INPUT_EXTENSION}")
    chunk_files = glob.glob(pattern)
    
    if not chunk_files:
        print(f"ERROR: No chunk files found matching pattern '{INPUT_PREFIX}*{INPUT_EXTENSION}'")
        return False
    
    print(f"Found {len(chunk_files)} chunk file(s)")
    
    # Sort files by chunk number
    chunk_data_list = []
    for chunk_file in chunk_files:
        chunk_num = extract_chunk_number(os.path.basename(chunk_file))
        if chunk_num is None:
            print(f"WARNING: Could not extract chunk number from {os.path.basename(chunk_file)}")
            continue
        chunk_data_list.append((chunk_num, chunk_file))
    
    # Sort by chunk number
    chunk_data_list.sort(key=lambda x: x[0])
    
    # Verify chunk sequence
    expected_chunks = list(range(1, len(chunk_data_list) + 1))
    actual_chunks = [num for num, _ in chunk_data_list]
    
    if expected_chunks != actual_chunks:
        print(f"WARNING: Chunk sequence may be incomplete!")
        print(f"  Expected: {expected_chunks}")
        print(f"  Found: {actual_chunks}")
        missing = set(expected_chunks) - set(actual_chunks)
        if missing:
            print(f"  Missing chunks: {sorted(missing)}")
    
    # Read and concatenate all chunks
    print("\nReading chunks...")
    base64_parts = []
    
    for chunk_num, chunk_file in chunk_data_list:
        print(f"  [{chunk_num}/{len(chunk_data_list)}] Reading {os.path.basename(chunk_file)}...", end=" ")
        
        chunk_data = read_chunk_from_word(chunk_file)
        
        if chunk_data is None:
            print("FAILED")
            return False
        
        base64_parts.append(chunk_data)
        print(f"OK ({len(chunk_data):,} bytes)")
    
    # Concatenate all base64 chunks
    print("\nConcatenating chunks...")
    full_base64 = ''.join(base64_parts)
    print(f"Total base64 size: {len(full_base64):,} bytes")
    
    # Decode base64 to binary
    print("Decoding base64 to binary...")
    try:
        binary_data = base64.b64decode(full_base64)
    except Exception as e:
        print(f"ERROR: Failed to decode base64: {str(e)}")
        return False
    
    print(f"Decoded binary size: {len(binary_data):,} bytes ({len(binary_data) / (1024*1024):.2f} MB)")
    
    # Calculate hash of reconstructed data
    reconstructed_hash = calculate_sha256(binary_data)
    print(f"Reconstructed file SHA256: {reconstructed_hash}")
    
    # Check if metadata file exists to verify hash
    metadata_path = os.path.join(input_dir, "metadata.txt")
    if os.path.exists(metadata_path):
        print("\nVerifying against original metadata...")
        with open(metadata_path, 'r') as f:
            metadata_content = f.read()
            # Extract original hash from metadata
            for line in metadata_content.split('\n'):
                if 'Original SHA256:' in line:
                    original_hash = line.split(':')[1].strip()
                    if original_hash == reconstructed_hash:
                        print(f"[OK] Hash verification PASSED!")
                        print(f"  Original:      {original_hash}")
                        print(f"  Reconstructed: {reconstructed_hash}")
                    else:
                        print(f"[FAIL] Hash verification FAILED!")
                        print(f"  Original:      {original_hash}")
                        print(f"  Reconstructed: {reconstructed_hash}")
                        print(f"  WARNING: Data may be corrupted!")
                    break
    
    # Write reconstructed tar file
    print(f"\nWriting reconstructed file to: {output_tar_path}")
    with open(output_tar_path, 'wb') as f:
        f.write(binary_data)
    
    print(f"\n[OK] Decoding complete!")
    print(f"[OK] Reconstructed file: {output_tar_path}")
    print(f"[OK] File size: {len(binary_data):,} bytes")
    print(f"[OK] SHA256: {reconstructed_hash}")
    
    return True

def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python ToMamdas_decode.py <input_directory> [output_tar_file]")
        print("\nExample:")
        print("  python ToMamdas_decode.py tomamdas_output")
        print("  python ToMamdas_decode.py tomamdas_output intelligraph_restored.tar")
        sys.exit(1)
    
    input_dir = sys.argv[1]
    output_tar = sys.argv[2] if len(sys.argv) > 2 else "intelligraph_restored.tar"
    
    try:
        success = decode_word_to_tar(input_dir, output_tar)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()

# Made with Bob
