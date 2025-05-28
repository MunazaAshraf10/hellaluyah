from fastapi import FastAPI, File, UploadFile, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
import os
import openai
import logging
from functools import wraps
import asyncio
import base64
import websockets
import uuid
import time
import boto3
import botocore
from botocore.exceptions import ClientError
import json
from datetime import datetime
import logging.handlers
from boto3 import resource
from boto3.dynamodb.conditions import Attr
import re
from cryptography.fernet import Fernet
import traceback

# Load environment variables
load_dotenv()

# Initialize logging with different handlers
log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
key = b'Vog2EOkP5ccjezrnyfpA1PGCs9EMkUXdHHxkY1VuGww='
# Initialize Fernet with the key
cipher = Fernet(key)


def encrypt_data(data):
    """
    Encrypts the given data using Fernet symmetric encryption.
    
    Args:
        data: The data to encrypt (must be bytes).
        
    Returns:
        Encrypted data as bytes.
    """
    return cipher.encrypt(data)

def decrypt_data(encrypted_data):
    """
    Decrypts the given data using Fernet symmetric encryption.
    
    Args:
        encrypted_data: The encrypted data (bytes, str, or Binary).
        
    Returns:
        Decrypted data as bytes.
    """
    # Add detailed logging to determine the type of data
    main_logger.info(f"Type of encrypted_data: {type(encrypted_data)}")
    
    # Handle nested dictionary with 'value' key - common format for stored encrypted values
    if isinstance(encrypted_data, dict) and 'value' in encrypted_data:
        main_logger.info("Data is a dictionary with 'value' key")
        encrypted_data = encrypted_data['value']
        if isinstance(encrypted_data, str):
            encrypted_data = encrypted_data.encode('utf-8')
    # For Binary objects from DynamoDB, don't try to log the actual value
    elif str(type(encrypted_data)) == "<class 'boto3.dynamodb.types.Binary'>":
        main_logger.info("Data is a DynamoDB Binary object")
        # Extract the bytes from the Binary object
        encrypted_data = encrypted_data.value
    elif isinstance(encrypted_data, str):
        encrypted_data = encrypted_data.encode('utf-8')
    elif isinstance(encrypted_data, bytes):
        pass  # Already in correct format
    elif isinstance(encrypted_data, dict) or isinstance(encrypted_data, list):
        # If it's a JSON structure that was stored, convert it to string first
        encrypted_data = json.dumps(encrypted_data).encode('utf-8')
    elif encrypted_data is None:
        raise ValueError("Encrypted data is None")
    else:
        raise TypeError(f"Encrypted data must be bytes or str, got {type(encrypted_data)}")

    # Proceed with decryption
    try:
        return cipher.decrypt(encrypted_data)
    except Exception as e:
        main_logger.error(f"Decryption error: {str(e)}")
        raise

# Configure logging handlers
def setup_logger(name, log_file, level=logging.INFO, format_string=None):
    if format_string is None:
        format_string = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    
    # Create directory if it doesn't exist
    log_dir = os.path.dirname(log_file)
    os.makedirs(log_dir, exist_ok=True)
    
    # Setup file handler with rotation
    handler = logging.handlers.RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(logging.Formatter(format_string))
    
    # Also log to console in development
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(format_string))
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Remove existing handlers to prevent duplicates
    if logger.handlers:
        logger.handlers.clear()
        
    logger.addHandler(handler)
    logger.addHandler(console)
    logger.propagate = False
    return logger

# Setup specific loggers
main_logger = setup_logger('main', f'{log_dir}/main.log')
time_logger = setup_logger('time', f'{log_dir}/time.log')
error_logger = setup_logger('error', f'{log_dir}/exceptions.log')
api_logger = setup_logger('api', f'{log_dir}/api.log')
db_logger = setup_logger('db', f'{log_dir}/database.log')

# Decorator for timing functions
def log_execution_time(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = datetime.now()
        try:
            result = await func(*args, **kwargs)
            end_time = datetime.now()
            execution_time = end_time - start_time
            time_logger.info(f'{func.__name__} took {execution_time.total_seconds():.2f} seconds to execute')
            return result
        except Exception as e:
            end_time = datetime.now()
            execution_time = end_time - start_time
            time_logger.error(f'{func.__name__} failed after {execution_time.total_seconds():.2f} seconds')
            raise
    return wrapper

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust for specific domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


client = openai.OpenAI(api_key=OPENAI_API_KEY)



# Initialize AWS clients
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET")

# Initialize AWS S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Initialize DynamoDB for metadata (optional but recommended)
dynamodb = boto3.resource(
    'dynamodb',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Add a new schema for discharge summaries
DISCHARGE_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "client": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dob": {"type": "string"},
                "discharge_date": {"type": "string"}
            }
        },
        "referral": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "reason": {"type": "string"}
            }
        },
        "presenting_issues": {"type": "array", "items": {"type": "string"}},
        "diagnosis": {"type": "array", "items": {"type": "string"}},
        "treatment_summary": {
            "type": "object",
            "properties": {
                "duration": {"type": "string"},
                "sessions": {"type": "string"},
                "therapy_type": {"type": "string"},
                "goals": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
                "medications": {"type": "string"}
            }
        },
        "progress": {
            "type": "object",
            "properties": {
                "overall": {"type": "string"},
                "goal_progress": {"type": "array", "items": {"type": "string"}}
            }
        },
        "clinical_observations": {
            "type": "object",
            "properties": {
                "engagement": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "challenges": {"type": "array", "items": {"type": "string"}}
            }
        },
        "risk_assessment": {"type": "string"},
        "outcome": {
            "type": "object",
            "properties": {
                "current_status": {"type": "string"},
                "remaining_issues": {"type": "string"},
                "client_perspective": {"type": "string"},
                "therapist_assessment": {"type": "string"}
            }
        },
        "discharge_reason": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "client_understanding": {"type": "string"}
            }
        },
        "discharge_plan": {"type": "string"},
        "recommendations": {
            "type": "object",
            "properties": {
                "overall": {"type": "string"},
                "followup": {"type": "array", "items": {"type": "string"}},
                "self_care": {"type": "array", "items": {"type": "string"}},
                "crisis_plan": {"type": "string"},
                "support_systems": {"type": "string"}
            }
        },
        "additional_notes": {"type": "string"},
        "final_note": {"type": "string"},
        "clinician": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date": {"type": "string"}
            }
        },
        "attachments": {"type": "array", "items": {"type": "string"}}
    }
}

# Add template schemas after the imports
CLINICAL_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "presenting_problems": {"type": "string"},
        "history_of_problems": {"type": "string"},
        "current_functioning": {"type": "string"},
        "current_medications": {"type": "string"},
        "psychiatric_history": {"type": "string"},
        "medical_history": {"type": "string"},
        "developmental_social_family_history": {"type": "string"},
        "substance_use": {"type": "string"},
        "cultural_religious_spiritual_issues": {"type": "string"},
        "risk_assessment": {"type": "string"},
        "mental_state_exam": {"type": "string"},
        "test_results": {"type": "string"},
        "diagnosis": {"type": "string"},
        "clinical_formulation": {"type": "string"}
    }
}

DETAILED_SOAP_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjective": {
            "type": "object",
            "properties": {
                "current_issues": {"type": "string"},
                "past_medical_history": {"type": "string"},
                "medications": {"type": "string"},
                "social_history": {"type": "string"},
                "allergies": {"type": "string"}
            }
        },
        "objective": {
            "type": "object",
            "properties": {
                "vital_signs": {"type": "string"},
                "physical_examination": {"type": "string"},
                "laboratory_results": {"type": "string"},
                "imaging_results": {"type": "string"},
                "other_diagnostics": {"type": "string"}
            }
        },
        "assessment": {
            "type": "object",
            "properties": {
                "diagnosis": {"type": "string"},
                "clinical_impression": {"type": "string"}
            }
        },
        "plan": {
            "type": "object",
            "properties": {
                "treatment": {"type": "string"},
                "patient_education": {"type": "string"},
                "referrals": {"type": "string"},
                "additional_instructions": {"type": "string"}
            }
        }
    }
}

NEW_SOAP_SCHEMA = {
    "type": "object",
    "properties": {
        "subjective": {
            "type": "object",
            "properties": {
                "reasons_and_complaints": {"type": "array", "items": {"type": "string"}},
                "duration_details": {"type": "string"},
                "modifying_factors": {"type": "string"},
                "progression": {"type": "string"},
                "previous_episodes": {"type": "string"},
                "impact_on_daily_activities": {"type": "string"},
                "associated_symptoms": {"type": "array", "items": {"type": "string"}}
            }
        },
        "past_medical_history": {
            "type": "object",
            "properties": {
                "contributing_factors": {"type": "string"},
                "exposure_history": {"type": "string"},
                "immunization_history": {"type": "string"},
                "other_relevant_info": {"type": "string"}
            }
        },
        "social_history": {"type": "string"},
        "family_history": {"type": "string"},
        "objective": {
            "type": "object",
            "properties": {
                "vital_signs": {
                    "type": "object",
                    "properties": {
                        "bp": {"type": "string"},
                        "hr": {"type": "string"},
                        "wt": {"type": "string"},
                        "t": {"type": "string"},
                        "o2": {"type": "string"},
                        "ht": {"type": "string"}
                    }
                },
                "physical_exam": {"type": "string"},
                "investigations": {"type": "string"}
            }
        },
        "assessment_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "assessment": {"type": "string"},
                    "differential_diagnosis": {"type": "array", "items": {"type": "string"}},
                    "investigations": {"type": "array", "items": {"type": "string"}},
                    "treatment": {"type": "array", "items": {"type": "string"}},
                    "referrals": {"type": "array", "items": {"type": "string"}},
                    "follow_up_plan": {"type": "string"}
                }
            }
        }
    }
}

SOAP_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjective": {
            "type": "object",
            "properties": {
                "reason_for_visit": {"type": "string"},
                "duration_timing": {"type": "string"},
                "alleviating_factors": {"type": "string"},
                "progression": {"type": "string"},
                "previous_episodes": {"type": "string"},
                "impact_on_daily_activities": {"type": "string"},
                "associated_symptoms": {"type": "string"}
            }
        },
        "past_medical_history": {
            "type": "object",
            "properties": {
                "contributing_factors": {"type": "string"},
                "social_history": {"type": "string"},
                "family_history": {"type": "string"},
                "exposure_history": {"type": "string"},
                "immunization_history": {"type": "string"},
                "other_relevant_info": {"type": "string"}
            }
        },
        "objective": {
            "type": "object",
            "properties": {
                "vital_signs": {"type": "object"},
                "physical_exam": {"type": "object"},
                "mental_state_exam": {"type": "object"},
                "investigations_results": {"type": "object"}
            }
        },
        "assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "differential_diagnosis": {"type": "string"}
                }
            }
        },
        "plan": {
            "type": "object",
            "properties": {
                "investigations_planned": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "treatment_plan": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "followup": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "referrals": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "other_actions": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        }
    }
}

CASE_FORMULATION_SCHEMA = {
    "type": "object",
    "properties": {
        "client_goals": {
            "type": ["string", "null"],
            "description": "The client's expressed goals and aspirations"
        },
        "presenting_problems": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "predisposing_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "precipitating_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "perpetuating_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "protective_factors": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "problem_list": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "treatment_goals": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        },
        "case_formulation": {
            "type": "string"
        },
        "treatment_mode": {
            "oneOf": [
                {
                    "type": "string"
                },
                {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            ]
        }
    },
    "required": ["client_goals", "presenting_problems", "predisposing_factors", 
                "precipitating_factors", "perpetuating_factors", "protective_factors", 
                "problem_list", "treatment_goals", "case_formulation", "treatment_mode"]
}

PROGRESS_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "clinic_details": {
            "type": "object",
            "properties": {
                "clinic_address": {"type": "string"},
                "contact_number": {"type": "string"},
                "fax_number": {"type": "string"},
                "practitioner_name": {"type": "string"}
            }
        },
        "patient_details": {
            "type": "object",
            "properties": {
                "surname": {"type": "string"},
                "first_name": {"type": "string"},
                "date_of_birth": {"type": "string"},
                "date_of_note": {"type": "string"}
            }
        },
        "clinical_content": {
            "type": "object",
            "properties": {
                "introduction": {"type": "string"},
                "history_and_status": {"type": "string"},
                "presentation": {"type": "string"},
                "mood_and_mental_state": {"type": "string"},
                "social_and_functional": {"type": "string"},
                "physical_health": {"type": "string"},
                "plan_and_recommendations": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "closing_statement": {"type": "string"}
            }
        }
    }
}

MENTAL_HEALTH_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_details": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dob": {"type": "string"},
                "consultation_date": {"type": "string"},
                "mrn": {"type": "string"}
            }
        },
        "reason_for_visit": {"type": "string"},
        "presenting_issue": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "string"},
                "duration": {"type": "string"},
                "impact": {"type": "string"}
            }
        },
        "past_psychiatric_history": {
            "type": "object",
            "properties": {
                "diagnoses": {"type": "string"},
                "treatments": {"type": "string"},
                "hospitalisations": {"type": "string"}
            }
        },
        "current_medications": {
            "type": "object",
            "properties": {
                "medications": {"type": "string"},
                "adherence": {"type": "string"},
                "side_effects": {"type": "string"}
            }
        },
        "mental_status": {
            "type": "object",
            "properties": {
                "appearance": {"type": "string"},
                "behavior": {"type": "string"},
                "speech": {"type": "string"},
                "mood": {"type": "string"},
                "affect": {"type": "string"},
                "thought_process": {"type": "string"},
                "thought_content": {"type": "string"},
                "cognition": {"type": "string"},
                "insight": {"type": "string"},
                "judgment": {"type": "string"}
            }
        },
        "assessment": {
            "type": "object",
            "properties": {
                "diagnosis": {"type": "string"},
                "severity": {"type": "string"}
            }
        },
        "treatment_plan": {
            "type": "object",
            "properties": {
                "medications": {"type": "string"},
                "therapy": {"type": "string"},
                "lifestyle": {"type": "string"},
                "follow_up": {"type": "string"}
            }
        },
        "safety_assessment": {
            "type": "object",
            "properties": {
                "suicide_risk": {"type": "string"},
                "self_harm_risk": {"type": "string"}
            }
        },
        "support": {
            "type": "object",
            "properties": {
                "support_systems": {"type": "string"},
                "emergency_contacts": {"type": "string"}
            }
        },
        "next_steps": {
            "type": "object",
            "properties": {
                "follow_up": {"type": "string"},
                "referrals": {"type": "string"},
                "additional_testing": {"type": "string"}
            }
        },
        "provider": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "title": {"type": "string"},
                "contact": {"type": "string"}
            }
        }
    }
}

# Add the new cardiology letter schema
CARDIOLOGY_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "doctor_details": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "credentials": {"type": "string"},
                "provider_number": {"type": "string"},
                "healthlink": {"type": "string"},
                "practice_address": {"type": "string"},
                "phone": {"type": "string"},
                "fax": {"type": "string"}
            }
        },
        "referral_details": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "referring_doctor": {"type": "string"},
                "practice_name": {"type": "string"},
                "practice_address": {"type": "string"},
                "file_number": {"type": "string"},
                "practice_phone": {"type": "string"},
                "practice_fax": {"type": "string"}
            }
        },
        "patient_details": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dob": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"},
                "mobile": {"type": "string"}
            }
        },
        "medical_history": {
            "type": "array",
            "items": {"type": "string"}
        },
        "medications": {"type": "string"},
        "consultation_notes": {"type": "string"},
        "examination_findings": {"type": "string"},
        "current_problems": {"type": "string"},
        "plan_recommendations": {"type": "string"},
        "closing": {"type": "string"},
        "disclaimer": {"type": "string"},
        "is_echocardiogram_report": {"type": "boolean"},
        "echocardiogram": {
            "type": "object",
            "properties": {
                "study_date": {"type": "string"},
                "study_number": {"type": "string"},
                "indication": {"type": "string"},
                "measurements": {"type": "object"},
                "findings": {
                    "type": "object",
                    "properties": {
                        "rhythm": {"type": "string"},
                        "left_ventricle": {"type": "string"},
                        "regional_wall": {"type": "string"},
                        "right_ventricle": {"type": "string"},
                        "left_atrium": {"type": "string"},
                        "right_atrium": {"type": "string"},
                        "aortic_valve": {"type": "string"},
                        "mitral_valve": {"type": "string"},
                        "tricuspid_valve": {"type": "string"},
                        "pericardium": {"type": "string"},
                        "aorta": {"type": "string"},
                        "additional": {"type": "string"}
                    }
                },
                "conclusions": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "string"},
                "sonographer": {"type": "string"}
            }
        }
    }
}

# Add the follow-up note schema
FOLLOWUP_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "presenting_complaints": {
            "type": "array",
            "items": {"type": "string"}
        },
        "mental_status": {
            "type": "object",
            "properties": {
                "appearance": {"type": "string"},
                "behavior": {"type": "string"},
                "speech": {"type": "string"},
                "mood": {"type": "string"},
                "affect": {"type": "string"},
                "thoughts": {"type": "string"},
                "perceptions": {"type": "string"},
                "cognition": {"type": "string"},
                "insight": {"type": "string"},
                "judgment": {"type": "string"}
            }
        },
        "risk_assessment": {"type": "string"},
        "diagnosis": {
            "type": "array",
            "items": {"type": "string"}
        },
        "treatment_plan": {"type": "string"},
        "safety_plan": {"type": "string"},
        "additional_notes": {"type": "string"}
    }
}


# Add the meeting minutes schema
MEETING_MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "time": {"type": "string"},
        "location": {"type": "string"},
        "attendees": {
            "type": "array",
            "items": {"type": "string"}
        },
        "agenda_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "discussion_points": {
            "type": "array",
            "items": {"type": "string"}
        },
        "decisions_made": {
            "type": "array",
            "items": {"type": "string"}
        },
        "action_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "next_meeting": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "time": {"type": "string"},
                "location": {"type": "string"}
            }
        }
    }
}

# Add the new consult schema
CONSULT_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "consultation_context": {
            "type": "object",
            "properties": {
                "consultation_type": {"type": "string"},  # F2F or T/C
                "patient_status": {"type": "string"},     # seen alone or with someone
                "reason_for_visit": {"type": "string"}
            }
        },
        "history": {
            "type": "object",
            "properties": {
                "presenting_complaints": {"type": "string"},
                "ideas_concerns_expectations": {"type": "string"},
                "red_flag_symptoms": {"type": "string"},
                "risk_factors": {"type": "string"},
                "past_medical_history": {"type": "string"},
                "medications": {"type": "string"},
                "allergies": {"type": "string"},
                "family_history": {"type": "string"},
                "social_history": {"type": "string"}
            }
        },
        "examination": {
            "type": "object",
            "properties": {
                "vital_signs": {"type": "string"},
                "physical_findings": {"type": "string"},
                "investigations": {"type": "string"}
            }
        },
        "impression": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "differential_diagnosis": {"type": "string"}
                }
            }
        },
        "plan": {
            "type": "object",
            "properties": {
                "investigations": {"type": "string"},
                "treatment": {"type": "string"},
                "referrals": {"type": "string"},
                "follow_up": {"type": "string"},
                "safety_netting": {"type": "string"}
            }
        },
        "consultation_date": {"type": "string"},
        "patient_name": {"type": "string"}
    }
}

# Add the new pathology schema
PATHOLOGY_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "therapy_attendance": {
            "type": "object",
            "properties": {
                "current_issues": {"type": "string"},
                "past_medical_history": {"type": "string"},
                "medications": {"type": "string"},
                "social_history": {"type": "string"},
                "allergies": {"type": "string"}
            }
        },
        "objective": {
            "type": "object",
            "properties": {
                "examination_findings": {"type": "string"},
                "diagnostic_tests": {"type": "string"}
            }
        },
        "reports": {"type": "string"},
        "therapy": {
            "type": "object",
            "properties": {
                "current_therapy": {"type": "string"},
                "therapy_changes": {"type": "string"}
            }
        },
        "outcome": {"type": "string"},
        "plan": {
            "type": "object",
            "properties": {
                "future_plan": {"type": "string"},
                "followup": {"type": "string"}
            }
        }
    }
}

# Add the new referral letter schema
REFERRAL_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "consultant": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "specialty": {"type": "string"},
                "hospital": {"type": "string"},
                "address": {"type": "string"}
            }
        },
        "patient": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dob": {"type": "string"},
                "condition": {"type": "string"},
                "phone": {"type": "string"},
                "email": {"type": "string"}
            }
        },
        "clinical_details": {
            "type": "object",
            "properties": {
                "presenting_complaint": {"type": "string"},
                "duration": {"type": "string"},
                "relevant_findings": {"type": "string"},
                "past_medical_history": {"type": "string"},
                "current_medications": {"type": "string"}
            }
        },
        "investigations": {
            "type": "object",
            "properties": {
                "recent_tests": {"type": "string"},
                "results": {"type": "string"}
            }
        },
        "reason_for_referral": {"type": "string"},
        "referring_doctor": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "title": {"type": "string"},
                "contact": {"type": "string"},
                "practice": {"type": "string"}
            }
        }
    }
}

# Define the schema for the dietician initial assessment
DIETICIAN_ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "weight_history": {"type": "string"},
        "body_image": {"type": "string"},
        "dietary_habits": {"type": "string"},
        "physical_activity": {"type": "string"},
        "medical_history": {"type": "string"},
        "medications": {"type": "string"},
        "supplements": {"type": "string"},
        "food_allergies": {"type": "string"},
        "cultural_dietary_preferences": {"type": "string"},
        "goals": {"type": "string"}
    }
}

# Define the schema for the psychology session notes
PSYCHOLOGY_SESSION_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "out_of_session_task_review": {"type": "string"},
        "current_presentation": {"type": "string"},
        "session_content": {"type": "string"},
        "intervention": {"type": "string"},
        "setbacks_barriers_progress": {"type": "string"},
        "risk_assessment_and_management": {"type": "string"},
        "mental_status_examination": {"type": "string"},
        "out_of_session_tasks": {"type": "string"},
        "plan_for_next_session": {"type": "string"}
    }
}

# Add a WebSocket connection manager
class ConnectionManager:
    """
    Manages WebSocket connections and associated session data.
    
    This class keeps track of active WebSocket connections and stores
    session-specific data like transcriptions. It provides methods to
    connect, disconnect, and send messages to clients.
    """
    def __init__(self):
        self.active_connections = {}  # Maps client_id to WebSocket instance
        self.session_data = {}  # Store session data including transcription for later processing

    async def connect(self, websocket: WebSocket, client_id: str):
        """
        Accept a new WebSocket connection and initialize session data.
        
        Args:
            websocket: The WebSocket connection to accept
            client_id: Unique identifier for the client
        """
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.session_data[client_id] = {
            "transcription": {
                "conversation": [],
                "metadata": {"duration": 0, "channels": 1}
            },
            "complete_transcript": [],
            "is_transcription_complete": False
        }

    def disconnect(self, client_id: str):
        """
        Remove a client's WebSocket connection but keep their session data.
        
        Args:
            client_id: Unique identifier for the client to disconnect
        """
        if client_id in self.active_connections:
            del self.active_connections[client_id]
        # Keep session data for potential processing after disconnect

    def get_session_data(self, client_id: str):
        """
        Retrieve session data for a specific client.
        
        Args:
            client_id: Unique identifier for the client
            
        Returns:
            Dictionary containing the client's session data
        """
        return self.session_data.get(client_id, {})

    def update_session_data(self, client_id: str, data):
        """
        Update session data for a specific client.
        
        Args:
            client_id: Unique identifier for the client
            data: New data to merge with existing session data
        """
        if client_id in self.session_data:
            self.session_data[client_id].update(data)

    async def send_message(self, message: str, client_id: str):
        """
        Send a text message to a specific client.
        
        Args:
            message: Message content to send
            client_id: Unique identifier for the recipient client
        """
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)

manager = ConnectionManager()

@app.get("/")
async def root():
    return {"message": "Welcome to the speech transcription and GPT-4 processing service!"}

@app.post("/upload-audio")
@log_execution_time
async def upload_audio(
    audio: UploadFile = File(...), 
    template_type: str = Form("clinical_report"),
    transcription_mode: str = Form("ai-summary")
):
    """
    Upload audio file for transcription and GPT-4 response generation.
    template_type options: "clinical_report", "soap_note", "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", "meeting_minutes"
    transcription_mode options: "word-to-word" or "ai-summary"
    """
    try:
        # Log incoming request details with more detail
        request_id = str(uuid.uuid4())[:8]  # Generate short request ID for tracking
        main_logger.info(f"[REQ-{request_id}] Received audio upload request - Filename: {audio.filename}, Content-Type: {audio.content_type}")
        main_logger.info(f"[REQ-{request_id}] Template Type received: '{template_type}' (type: {type(template_type).__name__})")
        main_logger.info(f"[REQ-{request_id}] Transcription Mode: '{transcription_mode}'")
        
        # Validate content type
        valid_content_types = [
            # WAV formats
            "audio/wav", "audio/wave", "audio/x-wav",
            # MP3 formats
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            # MP4/M4A formats
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            # FLAC formats
            "audio/flac", "audio/x-flac",
            # AAC formats
            "audio/aac", "audio/x-aac",
            # OGG formats
            "audio/ogg", "audio/vorbis", "application/ogg",
            # WEBM formats
            "audio/webm",
            # Other common audio formats
            "audio/3gpp", "audio/amr"
        ]

        if audio.content_type not in valid_content_types:
            # Allow files with missing content type but with audio file extensions
            valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
            file_extension = os.path.splitext(audio.filename.lower())[1]
            if file_extension in valid_extensions:
                main_logger.info(f"Audio content-type not recognized ({audio.content_type}), but filename has valid extension: {file_extension}")
            else:
                error_msg = f"Invalid audio format. Expected audio file, got {audio.content_type}"
                error_logger.error(error_msg)
                return JSONResponse({"error": error_msg}, status_code=400)

        # Validate template type
        valid_templates = ["clinical_report", "new_soap_note","detailed_soap_note","soap_note", "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", "meeting_minutes","referral_letter","detailed_dietician_initial_assessment","psychology_session_notes", "pathology_note", "consult_note","discharge_summary","case_formulation"]
        main_logger.info(f"[REQ-{request_id}] Valid templates: {valid_templates}")
        main_logger.info(f"[REQ-{request_id}] Is template_type in valid_templates? {template_type in valid_templates}")
        
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            return JSONResponse({"error": error_msg}, status_code=400)
        
        # Validate transcription mode
        valid_modes = ["word-to-word", "ai-summary"]
        if transcription_mode not in valid_modes:
            error_msg = f"Invalid transcription mode '{transcription_mode}'. Must be one of: {', '.join(valid_modes)}"
            error_logger.error(f"[REQ-{request_id}] {error_msg}")
            return JSONResponse({"error": error_msg}, status_code=400)
        
        main_logger.info(f"[REQ-{request_id}] Template type validation passed: {template_type}")

        # Read the audio file
        file_size = 0
        audio_data = bytearray()
        chunk_size = 1024 * 1024  # 1MB chunks
        
        while chunk := await audio.read(chunk_size):
            audio_data.extend(chunk)
            file_size += len(chunk)
            # Size limit removed

        if not audio_data:
            error_msg = "No audio data provided"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)

        main_logger.info(f"[REQ-{request_id}] Audio file read successfully. Size: {len(audio_data)} bytes")

        # Save audio to S3 first, so we have it even if processing fails
        audio_info = await save_audio_to_s3(bytes(audio_data))
        if not audio_info:
            error_msg = "Failed to save audio to S3"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=500)
            
        # Try to transcribe audio
        transcription_result = await transcribe_audio_with_diarization(bytes(audio_data))
        
        # If transcription fails, save the failure record but return error
        if isinstance(transcription_result, str) and transcription_result.startswith("Error"):
            await save_transcript_to_dynamodb(
                {"error": transcription_result},
                audio_info,
                status="failed"
            )
            return JSONResponse({"error": transcription_result}, status_code=400)
        
        # Initialize response variables
        ai_summary = None
        gpt_response = None
        formatted_report = None
        
        # Generate AI summary if requested
        if transcription_mode == "ai-summary":
            main_logger.info("Generating AI summary")
            summary_prompt = "Please provide a concise summary of the following medical conversation:\n\n"
            for entry in transcription_result["conversation"]:
                summary_prompt += f"{entry['speaker']}: {entry['text']}\n\n"
            
            try:
                summary_response = client.chat.completions.create(
                    model="gpt-4",
                    messages=[
                        {"role": "system", "content": "You are an expert medical documentation assistant. When summarizing conversations, do not use speaker labels like 'Speaker 0' or 'Speaker 1'. Instead, refer to participants by their roles (e.g., doctor/clinician and patient) based on context, or simply summarize the key medical information without attributing statements to specific speakers."},
                        {"role": "user", "content": summary_prompt}
                    ],
                    max_tokens=1000
                )
                
                ai_summary = summary_response.choices[0].message.content
                main_logger.info("AI summary generated successfully")
            except Exception as e:
                error_msg = f"Error generating AI summary: {str(e)}"
                error_logger.error(error_msg)
        
        # Generate template report
        main_logger.info(f"[REQ-{request_id}] Starting GPT template response generation")
        gpt_response = await generate_gpt_response(transcription_result, template_type)
        
        if isinstance(gpt_response, str) and gpt_response.startswith("Error"):
            # Update the transcript with successful transcription but failed processing
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                audio_info,
                status="partial"
            )
            main_logger.warning(f"[REQ-{request_id}] GPT processing failed but transcription succeeded. Transcript ID: {transcript_id}")
            return JSONResponse({"error": gpt_response, "transcript_id": transcript_id}, status_code=400)
        
        # Format the report
        formatted_report = await format_report(gpt_response, template_type)
        
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            # Update with partial success
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                audio_info,
                status="partial"
            )
            main_logger.warning(f"[REQ-{request_id}] Report formatting failed but transcription succeeded. Transcript ID: {transcript_id}")
            return JSONResponse({"error": formatted_report, "transcript_id": transcript_id}, status_code=400)
        
        # Store successful processing in AWS
        try:
            result = await save_to_aws(
                transcription_result,
                gpt_response,
                formatted_report,
                template_type,
                None,  # Audio already saved earlier
                status="completed"
            )
            
            if not result or 'transcript_id' not in result:
                main_logger.warning(f"[REQ-{request_id}] Failed to save complete data to AWS, but continuing with response")
                transcript_id = None
                report_id = None
            else:
                transcript_id = result['transcript_id']
                report_id = result['report_id']
                main_logger.info(f"[REQ-{request_id}] Successfully saved data. Transcript ID: {transcript_id}, Report ID: {report_id}")
        except Exception as e:
            error_logger.error(f"[REQ-{request_id}] AWS save error: {str(e)}", exc_info=True)
            transcript_id = None
            report_id = None
        
        # Build response
        response_data = {
            "transcription": transcription_result,
            "template_type": template_type,
            "transcription_mode": transcription_mode,
            "gpt_response": json.loads(gpt_response) if gpt_response else None,
            "formatted_report": formatted_report,
            "transcript_id": transcript_id,
            "report_id": report_id
        }
        
        if ai_summary:
            response_data["ai_summary"] = ai_summary
        
        main_logger.info(f"[REQ-{request_id}] Process completed successfully")
        return JSONResponse(response_data)

    except Exception as e:
        request_id = locals().get('request_id', str(uuid.uuid4())[:8])
        # Save the audio even if processing completely fails
        transcript_id = None
        if 'audio_data' in locals() and audio_data:
            audio_info = await save_audio_to_s3(bytes(audio_data))
            if audio_info:
                transcript_id = await save_transcript_to_dynamodb(
                    {"error": str(e)},
                    audio_info,
                    status="failed"
                )
                
        error_msg = f"Unexpected error in upload_audio: {str(e)}"
        error_logger.exception(f"[REQ-{request_id}] {error_msg}")
        return JSONResponse({"error": error_msg, "transcript_id": transcript_id}, status_code=500)

@app.post("/summarize-audio")
@log_execution_time
async def summarize_audio(
    audio: UploadFile = File(...), 
):
    """
    Upload audio file for transcription and GPT-4 summary generation.
    This endpoint performs transcription and AI summary in one step
    rather than streaming transcription.
    
    Args:
        audio: WAV audio file to process
    """
    try:
        # Log incoming request details
        main_logger.info(f"Received audio summary request - Filename: {audio.filename}, Content-Type: {audio.content_type}")

        # Validate content type
        valid_content_types = [
            # WAV formats
            "audio/wav", "audio/wave", "audio/x-wav",
            # MP3 formats
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            # MP4/M4A formats
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            # FLAC formats
            "audio/flac", "audio/x-flac",
            # AAC formats
            "audio/aac", "audio/x-aac",
            # OGG formats
            "audio/ogg", "audio/vorbis", "application/ogg",
            # WEBM formats
            "audio/webm",
            # Other common audio formats
            "audio/3gpp", "audio/amr"
        ]

        if audio.content_type not in valid_content_types:
            # Allow files with missing content type but with audio file extensions
            valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
            file_extension = os.path.splitext(audio.filename.lower())[1]
            if file_extension in valid_extensions:
                main_logger.info(f"Audio content-type not recognized ({audio.content_type}), but filename has valid extension: {file_extension}")
            else:
                error_msg = f"Invalid audio format. Expected audio file, got {audio.content_type}"
                error_logger.error(error_msg)
                return JSONResponse({"error": error_msg}, status_code=400)

        # Read the audio file
        file_size = 0
        audio_data = bytearray()
        chunk_size = 1024 * 1024  # 1MB chunks
        
        while chunk := await audio.read(chunk_size):
            audio_data.extend(chunk)
            file_size += len(chunk)
            # Size limit removed

        if not audio_data:
            error_msg = "No audio data provided"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)

        main_logger.info(f"Audio file read successfully. Size: {len(audio_data)} bytes")

        # Always save audio to S3 first
        audio_info = await save_audio_to_s3(bytes(audio_data))
        if not audio_info:
            error_msg = "Failed to save audio to S3"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=500)
            
        # Transcribe audio - with better diarization settings
        transcription_result = await transcribe_audio_with_diarization(bytes(audio_data))
        
        # Save basic info to DynamoDB even if transcription fails
        transcript_id = None
        if isinstance(transcription_result, str) and transcription_result.startswith("Error"):
            transcript_id = await save_transcript_to_dynamodb(
                {"error": transcription_result},
                audio_info,
                status="failed"
            )
            return JSONResponse({
                "error": transcription_result, 
                "transcript_id": transcript_id,
                "audio_info": audio_info
            }, status_code=400)
        
        # Generate AI summary
        main_logger.info("Generating AI summary")
        summary_prompt = "Please provide a concise summary of the following medical conversation:\n\n"
        for entry in transcription_result["conversation"]:
            summary_prompt += f"{entry['speaker']}: {entry['text']}\n\n"
        
        try:
            summary_response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert medical documentation assistant. When summarizing conversations, do not use speaker labels like 'Speaker 0' or 'Speaker 1'. Instead, refer to participants by their roles (e.g., doctor/clinician and patient) based on context, or simply summarize the key medical information without attributing statements to specific speakers."},
                    {"role": "user", "content": summary_prompt}
                ],
                # max_tokens=1000
            )
            
            ai_summary = summary_response.choices[0].message.content
            main_logger.info("AI summary generated successfully")
            
            # Save complete results to database first to get transcript_id
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                audio_info,
                status="completed"
            )
            if not transcript_id:
                error_msg = "Failed to save transcription to DynamoDB"
                error_logger.error(error_msg)
                return JSONResponse({"error": error_msg}, status_code=500)
            
            # Save the summary to DynamoDB using the obtained transcript_id
            summary_id = await save_summary_to_dynamodb(ai_summary, transcript_id)
            
        except Exception as e:
            error_msg = f"Error generating AI summary: {str(e)}"
            error_logger.error(error_msg)
            # Save partial results
            transcript_id = await save_transcript_to_dynamodb(
                transcription_result,
                audio_info,
                status="partial"
            )
            return JSONResponse({
                "error": error_msg, 
                "transcript_id": transcript_id,
                "transcription": transcription_result,
                "audio_info": audio_info
            }, status_code=500)
        
        # Return the response
        response_data = {
            "transcription": transcription_result,
            "ai_summary": ai_summary,
            "summary_id": summary_id,
            "transcript_id": transcript_id,
            "audio_info": audio_info
        }
        
        main_logger.info(f"Audio summary process completed successfully")
        return JSONResponse(response_data)

    except Exception as e:
        # Save the audio even if processing completely fails
        transcript_id = None
        if 'audio_data' in locals() and audio_data:
            audio_info = await save_audio_to_s3(bytes(audio_data))
            if audio_info:
                transcript_id = await save_transcript_to_dynamodb(
                    {"error": str(e)},
                    audio_info,
                    status="failed"
                )
                
        error_msg = f"Unexpected error in summarize_audio: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({
            "error": error_msg, 
            "transcript_id": transcript_id
        }, status_code=500)

@app.post("/generate-template-report")
@log_execution_time
async def generate_template_report(
    transcript_id: str = Form(...),
    template_type: str = Form("clinical_report")
):
    """
    Generate a formatted report using a specific template based on an existing transcript.
    This endpoint can be used after either live transcription or AI summary to create the final report.
    
    Args:
        transcript_id: ID of the transcript to use for report generation
        template_type: Type of template report to generate
    """
    try:
        # Validate template type
        valid_templates = ["clinical_report","new_soap_note", "detailed_soap_note","soap_note", "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", "meeting_minutes","referral_letter","detailed_dietician_initial_assessment","psychology_session_notes","pathology_note", "consult_note","discharge_summary","case_formulation"]
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)
            
        # Get transcript data
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
        transcript_item = response['Item']
        
        # Get transcript data
        transcript_data = transcript_item.get('transcript', '{}')

        # Check if it's a Binary object and decrypt if needed
        if str(type(transcript_data)) == "<class 'boto3.dynamodb.types.Binary'>":
            transcript_data = decrypt_data(transcript_data)
            # Decode bytes to string after decryption
            transcript_data = transcript_data.decode('utf-8')
        elif isinstance(transcript_data, bytes):
            transcript_data = decrypt_data(transcript_data).decode('utf-8')
        print(f"Transcript data: {transcript_data}")
        # Now parse the JSON
        try:
            transcription = json.loads(transcript_data)
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid transcript data format"},
                status_code=400
            )
            
        # Generate GPT response
        main_logger.info(f"Generating {template_type} template for transcript {transcript_id}")
        gpt_response = await generate_gpt_response(transcription, template_type)
        
        if isinstance(gpt_response, str) and gpt_response.startswith("Error"):
            return JSONResponse({"error": gpt_response}, status_code=400)
        
        # Format the report
        formatted_report = await format_report(gpt_response, template_type)
        
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            return JSONResponse({"error": formatted_report}, status_code=400)
        
        # Save report to database
        report_id = await save_report_to_dynamodb(
            transcript_id,
            gpt_response,
            formatted_report,
            template_type,
            status="completed"
        )
        
        # Return the formatted report
        response_data = {
            "report_id": report_id,
            "template_type": template_type,
            "gpt_response": json.loads(gpt_response) if gpt_response else None,
            "formatted_report": formatted_report
        }
        
        main_logger.info(f"Template report generation completed successfully")
        return JSONResponse(response_data)
        
    except Exception as e:
        error_msg = f"Unexpected error in generate_template_report: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

# Update WebSocket endpoint to focus just on live transcription without template generation
@app.websocket("/ws/live-transcription")
async def live_transcription_endpoint(websocket: WebSocket):
    client_id = str(uuid.uuid4())
    print(f"\n🔌 New WebSocket connection request from client {client_id}")
    print(f"📡 Client address: {websocket.client.host}:{websocket.client.port}")
    
    try:
        await manager.connect(websocket, client_id)
        print(f"✅ Client {client_id} connected successfully")
        
        # Wait for configuration message from client
        print(f"⏳ Waiting for configuration from client {client_id}")
        config_msg = await websocket.receive_text()
        print(f"📥 Raw config message: {config_msg}")
        
        try:
            config = json.loads(config_msg)
            print(f"📝 Parsed configuration: {json.dumps(config, indent=2)}")
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse configuration: {e}")
            print(f"Raw message was: {config_msg}")
            raise
        
        # Acknowledge configuration
        response = {
            "status": "ready",
            "message": "Ready to receive audio for real-time transcription"
        }
        print(f"📤 Sending response: {json.dumps(response, indent=2)}")
        await websocket.send_text(json.dumps(response))
        print(f"✅ Sent ready status to client {client_id}")
        
        # Initialize session data
        session_data = manager.get_session_data(client_id)
        print(f"📊 Initialized session data for client {client_id}")
        
        # Create an in-memory buffer to store audio data for S3 upload
        audio_buffer = bytearray()
        
        # Connect to Deepgram WebSocket
        print(f"🔌 Connecting to Deepgram for client {client_id}")
        deepgram_url = f"wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1&model=nova-2-medical&language=en&diarize=true&punctuate=true&smart_format=true"
        print(f"🌐 Deepgram URL: {deepgram_url}")
        
        try:
            deepgram_socket = await websockets.connect(
                deepgram_url,
                additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            print(f"✅ Connected to Deepgram for client {client_id}")
        except Exception as e:
            print(f"❌ Failed to connect to Deepgram: {str(e)}")
            print(f"Error details: {traceback.format_exc()}")
            raise

        # Start processing tasks
        print(f"🚀 Starting processing tasks for client {client_id}")
        await asyncio.gather(
            process_audio_stream(websocket, deepgram_socket, audio_buffer),
            process_transcription_results(deepgram_socket, websocket, client_id)
        )

        # --- NEW: After streaming ends, save audio and transcript ---
        print(f"💾 Saving complete audio and transcript for client {client_id}")
        # Save audio to S3
        audio_info = await save_audio_to_s3(bytes(audio_buffer))
        if not audio_info:
            print(f"❌ Failed to save audio to S3 for client {client_id}")
        else:
            print(f"✅ Audio saved to S3 for client {client_id}: {audio_info}")

        # Save transcript to DynamoDB
        session_data = manager.get_session_data(client_id)
        transcript_data = session_data.get("transcription", {})
        transcript_id = await save_transcript_to_dynamodb(
            transcript_data,
            audio_info,
            status="completed"
        )
        print(f"✅ Transcript saved to DynamoDB for client {client_id} with transcript_id: {transcript_id}")
        print(f"Transcript data: {transcript_data}")
        # --- NEW: Send transcript_id to client ---
        await websocket.send_text(json.dumps({
            "type": "transcription_complete",
            "transcript_id": transcript_id
        }))

    except WebSocketDisconnect:
        print(f"❌ Client {client_id} disconnected")
        manager.disconnect(client_id)
    except Exception as e:
        print(f"❌ Error in live transcription for client {client_id}: {str(e)}")
        print(f"Error details: {traceback.format_exc()}")
        manager.disconnect(client_id)
    finally:
        print(f"🧹 Cleaning up resources for client {client_id}")
        manager.disconnect(client_id)

async def process_audio_stream(websocket: WebSocket, deepgram_socket, audio_buffer=None):
    """Process incoming audio stream from client and forward to Deepgram."""
    try:
        while True:
            print("🎤 Waiting for audio data from client...")
            try:
                message = await websocket.receive()
                if "bytes" in message:
                    data = message["bytes"]
                    print(f"📥 Received {len(data)} bytes of audio data")
                    
                    if audio_buffer is not None:
                        audio_buffer.extend(data)
                    
                    print("📤 Forwarding audio data to Deepgram...")
                    await deepgram_socket.send(data)
                    print("✅ Audio data forwarded to Deepgram")
                elif "text" in message:
                    # Handle text messages (like end_audio signal)
                    text_data = message["text"]
                    if text_data == '{"type":"end_audio"}':
                        print("📤 Sending end_audio signal to Deepgram")
                        await deepgram_socket.send(text_data)
                        print("✅ End audio signal sent to Deepgram")
                        break
            except WebSocketDisconnect:
                print("❌ Client disconnected")
                break
            except Exception as e:
                print(f"❌ Error processing message: {str(e)}")
                print(f"Error details: {traceback.format_exc()}")
                break
            
    except Exception as e:
        print(f"❌ Error in process_audio_stream: {str(e)}")
        print(f"Error details: {traceback.format_exc()}")
        raise

async def process_transcription_results(deepgram_socket, websocket, client_id):
    """Process transcription results from Deepgram and send to client."""
    try:
        while True:
            try:
                print("⏳ Waiting for transcription from Deepgram...")
                response = await deepgram_socket.recv()
                print(f"📥 Received response from Deepgram: {response}")
                
                if isinstance(response, str):
                    data = json.loads(response)
                    
                    if "channel" in data and "alternatives" in data["channel"]:
                        transcript = data["channel"]["alternatives"][0]
                        is_final = data.get("is_final", False)
                        
                        # Extract speaker if available
                        speaker = "Speaker 1"  # Default speaker
                        if "words" in transcript and transcript["words"]:
                            if "speaker" in transcript["words"][0]:
                                speaker = f"Speaker {transcript['words'][0]['speaker']}"
                        
                        message = {
                            "type": "transcript",
                            "text": transcript["transcript"],
                            "speaker": speaker,
                            "is_final": is_final
                        }
                        
                        print(f"📤 Sending transcription to client {client_id}:")
                        print(f"   Speaker: {speaker}")
                        print(f"   Text: {transcript['transcript']}")
                        print(f"   Is Final: {is_final}")
                        
                        await websocket.send_text(json.dumps(message))
                        print("✅ Transcription sent to client")

                        # Only process if it's a transcript
                        if data.get("type") == "Results":
                            # Extract the transcript text and speaker
                            transcript_text = data["channel"]["alternatives"][0]["transcript"]
                            speaker = data["channel"]["alternatives"][0].get("speaker", "Speaker 0")
                            is_final = data.get("is_final", False)

                            # Add to session data
                            session_data = manager.get_session_data(client_id)
                            if "transcription" not in session_data:
                                session_data["transcription"] = {"conversation": []}
                            if transcript_text.strip():
                                session_data["transcription"]["conversation"].append({
                                    "speaker": speaker,
                                    "text": transcript_text,
                                    "is_final": is_final
                                })
                            manager.update_session_data(client_id, session_data)
            except websockets.exceptions.ConnectionClosed:
                print("❌ Deepgram connection closed")
                break
            except Exception as e:
                print(f"❌ Error processing Deepgram response: {str(e)}")
                print(f"Error details: {traceback.format_exc()}")
                break
            
    except Exception as e:
        print(f"❌ Error in process_transcription_results: {str(e)}")
        print(f"Error details: {traceback.format_exc()}")
        raise

@log_execution_time
async def transcribe_audio_with_diarization(audio_data):
    """
    Transcribe audio with speaker diarization using Deepgram API.
    
    This function sends audio data to Deepgram's API for transcription
    with speaker diarization (identifying different speakers). It processes
    the API response to structure the conversation with speaker labels.
    
    Args:
        audio_data: Binary audio data (WAV format) to transcribe
        
    Returns:
        Dictionary containing the transcribed conversation with speaker information,
        or an error message string if transcription failed
    """
    try:
        # Validate API key
        if not DEEPGRAM_API_KEY:
            error_logger.error("Deepgram API key is missing")
            return "Error: Deepgram API key is not configured"

        url = "https://api.deepgram.com/v1/listen"
        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "audio/wav",
            "Accept": "application/json"
        }

        # Enhanced parameters with stronger diarization settings
        params = {
            "topics": True,
            "smart_format": True,
            "punctuate": True,
            "utterances": True,
            "utt_split": 0.6,           # More aggressive utterance splitting
            "diarize": True,
            "diarization": {
                "speakers": 2,          # Explicitly indicate 2 speakers
                "sensitivity": "high"   # Increase sensitivity to speaker changes
            },
            "sentiment": True,
            "language": "en",
            "model": "nova-2-medical"
        }

        main_logger.info(f"Processing audio data of size: {len(audio_data)} bytes")
        main_logger.info(f"Sending request to Deepgram API with enhanced diarization settings")

        # Make async request to Deepgram API
        async with httpx.AsyncClient(timeout=1200.0) as client:
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    params=params,
                    content=audio_data
                )
                
                main_logger.info(f"Deepgram API Response Status: {response.status_code}")

                if response.status_code != 200:
                    error_msg = response.headers.get('dg-error', 'Unknown error occurred')
                    error_logger.error(f"Deepgram API error: {error_msg}")
                    return f"Error in transcription: {error_msg}"

                try:
                    response_json = response.json()
                except json.JSONDecodeError as e:
                    error_logger.error(f"Failed to decode JSON response: {str(e)}")
                    return "Error: Invalid JSON response from transcription service"

                # Process results
                if 'results' not in response_json:
                    error_logger.error("Missing 'results' in API response")
                    return "Error: Invalid response structure from transcription service"

                results = response_json['results']
                channels = results.get('channels', [])
                
                if not channels:
                    error_logger.error("No channels found in transcription results")
                    return "Error: No transcription data found"
                
                channel = channels[0]
                alternatives = channel.get('alternatives', [])
                
                if not alternatives:
                    error_logger.error("No alternatives found in transcription results")
                    return "Error: No transcription alternatives found"

                # Prepare structure for the formatted response
                formatted_response = {
                    "conversation": [],
                    "metadata": {
                        "duration": results.get('duration', 0),
                        "channels": len(channels),
                        "models": results.get('models', [])
                    }
                }

                # Process utterances with detailed logging
                alternative = alternatives[0]
                
                # Primarily try to use utterances for speaker separation
                if 'utterances' in alternative:
                    utterances = alternative['utterances']
                    main_logger.info(f"Found {len(utterances)} utterances")
                    
                    for utterance in utterances:
                        speaker = utterance.get('speaker', 0)
                        text = utterance.get('transcript', '').strip()
                        
                        if text:
                            formatted_response["conversation"].append({
                                "speaker": f"Speaker {speaker}",
                                "text": text
                            })
                elif 'words' in alternative:
                    # Fallback: use word-level diarization
                    words = alternative.get('words', [])
                    main_logger.info(f"Using word-level diarization with {len(words)} words")
                    
                    if words:
                        current_speaker = None
                        current_text = []
                        
                        # Group consecutive words by the same speaker
                        for word in words:
                            speaker = word.get('speaker')
                            if speaker != current_speaker and current_text:
                                # New speaker detected, save previous utterance
                                formatted_response["conversation"].append({
                                    "speaker": f"Speaker {current_speaker or 0}",
                                    "text": " ".join(current_text).strip()
                                })
                                current_text = []
                            current_speaker = speaker
                            current_text.append(word.get('word', ''))
                        
                        # Add final utterance
                        if current_text:
                            formatted_response["conversation"].append({
                                "speaker": f"Speaker {current_speaker or 0}",
                                "text": " ".join(current_text).strip()
                            })
                else:
                    # Final fallback - just use the entire transcript with no speaker distinction
                    transcript = alternative.get('transcript', '')
                    if transcript:
                        formatted_response["conversation"].append({
                            "speaker": "Speaker 0",
                            "text": transcript
                        })

                main_logger.info(f"Formatted {len(formatted_response['conversation'])} conversation segments")
                return formatted_response

            except httpx.TimeoutException:
                error_logger.error("Request timed out")
                return "Error: Request timed out"
            except httpx.RequestError as e:
                error_logger.error(f"Request failed: {str(e)}")
                return f"Error: Request failed - {str(e)}"

    except Exception as e:
        error_logger.exception("Unexpected error in transcription")
        return f"Error in transcription: {str(e)}"

@log_execution_time
async def generate_gpt_response(transcription, template_type):
    """
    Generate GPT response based on a transcription and template type.
    
    Args:
        transcription: Dictionary containing the transcription data
        template_type: Type of template to use for formatting
        
    Returns:
        GPT response in JSON format
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        main_logger.info(f"[OP-{operation_id}] Generating GPT response for template: {template_type}")
        
        # Prepare conversation text for the prompt
        conversation_text = ""
        if "conversation" in transcription:
            for entry in transcription["conversation"]:
                speaker = entry.get("speaker", "Unknown")
                text = entry.get("text", "")
                conversation_text += f"{speaker}: {text}\n\n"
        
        # Select system message based on template type
        system_message = "You are a clinical documentation expert. Format the conversation into structured clinical notes."

        # Core instructions for all medical documentation templates
        preservation_instructions = """
CRITICAL INSTRUCTIONS:
1. ALWAYS preserve ALL dates mentioned (appointment dates, symptom onset, follow-up dates, etc.)
2. NEVER omit medication names, dosages, frequencies or routes of administration
3. Preserve ALL quantitative values including lab results, vital signs, and measurements
4. Include specific timeframes for symptom progression (e.g., "for 2 weeks", "since April 10th")
5. Maintain ALL numeric values exactly as stated (weights, blood pressure readings, glucose levels)
6. ALWAYS include complete diagnostic information and specific medical terminology
7. Preserve all references to previous treatments, procedures or interventions
8. Extract and include all allergies, adverse reactions, and contraindications
"""

        # Add grammar-specific instructions to all templates
        grammar_instructions = """
CRITICAL GRAMMAR INSTRUCTIONS:
1. Use perfect standard US English grammar in all text
2. Ensure proper subject-verb agreement in all sentences
3. Use appropriate prepositions (avoid errors like "dependent of" instead of "dependent on")
4. Eliminate article errors (never use "the a" or similar incorrect article combinations)
5. Maintain professional medical writing style with proper punctuation
6. Use proper tense consistency throughout all documentation
7. Ensure proper noun-pronoun agreement
8. Use clear and concise phrasing without grammatical ambiguity
"""

        # Template-specific instructions and schema
        if template_type == "soap_note":
            # SOAP Note Instructions - Updated for conciseness and direct language
            system_message = f"""You are a medical documentation expert specializing in SOAP notes. 
{preservation_instructions}
{grammar_instructions}

CRITICAL STYLE INSTRUCTIONS:
1. Be CONCISE and DIRECT in your documentation (e.g., "Lost 2kg in 3 weeks" NOT "The patient reported that they have lost two kilograms over the past three weeks")
2. Use medical shorthand where appropriate (e.g., "BP 120/80" instead of "Blood pressure is 120/80")
3. Format all assessment issues as a numbered list with clear issue, diagnosis, and differential diagnosis
4. Focus on capturing critical details using the fewest words necessary
5. Use bullet-point style phrasing rather than complete sentences when possible

When formatting the provided medical conversation, structure it according to the SOAP format:
- Subjective: Patient's statements, symptoms, and concerns WITH ALL DATES and timelines preserved exactly
- Objective: ONLY include vital signs, physical examination findings, and completed investigations WITH RESULTS that are explicitly mentioned. DO NOT include planned investigations here.
- Assessment: Medical assessment and diagnoses as numbered issues with clear diagnoses
- Plan: Treatment plan WITH ALL medication names, doses, and schedules preserved exactly. Include planned investigations here, not in Objective.

Format your response as a valid JSON object according to this schema:
{{
  "subjective": {{
    "chief_complaint": "CONCISE string with chief complaints and all dates",
    "history_of_present_illness": "CONCISE string with timeline and all dates",
    "current_medications": "CONCISE list of ALL medications with EXACT dosages",
    "allergies": "CONCISE string",
    "past_medical_history": "CONCISE string",
    "family_history": "CONCISE string",
    "social_history": "CONCISE string",
    "review_of_systems": "CONCISE string"
  }},
  "past_medical_history": {{
    "medical_conditions": "CONCISE string",
    "surgical_history": "CONCISE string",
    "family_history": "CONCISE string",
    "social_history": "CONCISE string",
    "mental_health_history": "CONCISE string",
    "environmental_factors": "CONCISE string",
    "other_factors": "CONCISE string"
  }},
  "objective": {{
    "vital_signs": {{
      "blood_pressure": "DIRECT string with exact values if mentioned",
      "heart_rate": "DIRECT string with exact values if mentioned",
      "respiratory_rate": "DIRECT string with exact values if mentioned",
      "temperature": "DIRECT string with exact values if mentioned",
      "oxygen_saturation": "DIRECT string with exact values if mentioned",
      "weight": "DIRECT string with exact values if mentioned",
      "height": "DIRECT string with exact values if mentioned",
      "bmi": "DIRECT string with exact values if mentioned",
      "pain_level": "DIRECT string with exact values if mentioned"
    }},
    "physical_exam": {{
      "findings": "CONCISE string with any examination findings"
    }},
    "investigations_results": {{
      "completed_investigations": "CONCISE string with results of COMPLETED investigations only"
    }}
  }},
  "assessment": [
    {{
      "issue": "CONCISE string describing problem",
      "diagnosis": "CONCISE string with formal diagnosis if made",
      "differential_diagnosis": "CONCISE string with alternative diagnoses being considered"
    }}
  ],
  "plan": {{
    "investigations_planned": ["Array of planned tests/investigations WITH clear context (e.g., 'Blood tests ordered', 'Physical examination scheduled') and exact dates"],
    "treatment_plan": ["Array of treatments WITH clear action context (e.g., 'Prescribed', 'Advised to', 'Recommended') and all medication details, dosages, and schedules"],
    "followup": ["Array of followup plans WITH clear description (e.g., 'Follow-up appointment scheduled', 'Requested to return if symptoms worsen') and specific dates"],
    "referrals": ["Array of referrals WITH clear status (e.g., 'Referred to', 'Referral planned to') and timeframes"],
    "other_actions": ["Array of other actions WITH clear descriptive context about what was recommended or planned"]
  }}
}}

For the assessment section, format each issue as follows:
1. First issue with diagnosis and differential diagnosis (if available)
2. Second issue with diagnosis and differential diagnosis (if available)
3. And so on...

IMPORTANT: 
- Your response MUST be a valid JSON object exactly matching this schema
- Use "Not discussed" for any field without information in the conversation
- Do not invent information not present in the conversation
- Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
- Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
- Only include COMPLETED investigations under Objective; planned investigations go under Plan
- For plan items, add explanatory context to make them more descriptive (e.g., "Physical examination scheduled" instead of just "Physical examination")
"""
        
        elif template_type == "referral_letter":
            system_message = f"""You are a medical documentation expert specializing in professional referral letters.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a doctor would write - using professional medical terminology and standard medical abbreviations
        2. Be CONCISE and DIRECT in your documentation 
        3. Use FULL SENTENCES, never bullet points in the final document
        4. Format the letter exactly according to the structure provided
        5. Only include information explicitly mentioned in the transcript
        6. Always preserve all dates, medication dosages, and measurements EXACTLY as stated
        7. When information is not available in the transcript, do not include placeholder text - simply leave that section blank

        Format your response as a valid JSON object according to this schema:
        {{
        "date": "Current date if no date is mentioned in the format DD Month YYYY (e.g., '4 April 2025')",
        "consultant": {{
            "name": "Consultant's name if mentioned",
            "specialty": "Consultant's specialty or department if mentioned",
            "hospital": "Hospital or clinic name if mentioned",
            "address": "Hospital address if mentioned"
        }},
        "patient": {{
            "name": "Patient's full name if mentioned",
            "dob": "Patient's date of birth if mentioned",
            "condition": "The specific condition requiring referral",
            "phone": "Patient's phone number if mentioned",
            "email": "Patient's email address if mentioned"
        }},
        "clinical_details": {{
            "presenting_complaint": "Concise description of presenting complaint WITH EXACT TIMELINE",
            "duration": "Duration of symptoms WITH EXACT TIMEFRAME",
            "relevant_findings": "Relevant physical or clinical findings",
            "past_medical_history": "Past medical history with ALL CONDITIONS preserved",
            "current_medications": "Current medications with EXACT DOSAGES preserved"
        }},
        "investigations": {{
            "recent_tests": "Recent tests performed with DATES if mentioned",
            "results": "Test results with EXACT VALUES preserved"
        }},
        "reason_for_referral": "Detailed reason for referral including any urgency indicators",
        "referring_doctor": {{
            "name": "Referring doctor's name if mentioned",
            "title": "Referring doctor's professional title if mentioned",
            "contact": "Referring doctor's contact information if mentioned",
            "practice": "Referring doctor's practice name if mentioned"
        }}
    }}
    IMPORTANT: 
    - Your response MUST be a valid JSON object exactly matching this schema
    - Only include information explicitly mentioned in the transcript
    - If information for a field is not mentioned, provide an empty string
    - Do not invent information not present in the conversation
    - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
    - Use professional medical terminology
    """


        elif template_type == "consult_note":
            # Add the new system prompt for consultation notes
            system_message = f"""You are a medical documentation expert specializing in concise and professional consultation notes.
{preservation_instructions}
{grammar_instructions}

CRITICAL STYLE INSTRUCTIONS:
1. Write as a doctor would write - using professional medical terminology and standard medical abbreviations
2. Be CONCISE and DIRECT in your documentation (e.g., "2-week history of productive cough" NOT "The patient reported that they have been experiencing a cough with phlegm for the past two weeks")
3. Use medical shorthand where appropriate (e.g., "BP 120/80" instead of "Blood pressure is 120/80")
4. Document each issue separately with clear diagnosis and differential diagnosis
5. Focus on capturing critical details using the fewest words necessary
6. Format the note exactly according to the structure provided
7. When information is not present in the transcript, indicate this with an appropriate note (e.g., "Not documented during consultation")

CRITICAL FORMATTING RULES FOR IMPRESSION SECTION:
1. SEPARATE each medical issue or symptom into its OWN impression item
2. DO NOT combine multiple symptoms into a single impression item
3. Each impression item should focus on ONE specific issue (e.g., "Shortness of breath" or "Peripheral edema")
4. For EACH separate issue, provide its likely diagnosis
5. For multiple related symptoms that suggest a single diagnosis, create ONE impression item for each primary symptom
6. Example format:
   Issue 1: "Shortness of breath" - Diagnosis: "Heart failure"
   Issue 2: "Peripheral edema" - Diagnosis: "Likely secondary to heart failure"

When formatting the provided medical conversation, structure it according to this consultation note format:
- Consultation Context: Whether F2F (face-to-face) or T/C (telephone consultation), who was present, and reason for visit
- History: Patient's presenting complaints, ideas/concerns/expectations, red flag symptoms, risk factors, past medical history, medications, allergies, family history, and social history
- Examination: Vital signs, physical examination findings, and any investigations with results
- Impression: SEPARATE numbered issues with diagnosis and differential diagnosis
- Plan: Investigations, treatment, referrals, follow-up plan, and safety netting advice

Format your response as a valid JSON object according to this schema:
{{
  "consultation_context": {{
    "consultation_type": "String indicating F2F or T/C",
    "patient_status": "String indicating if seen alone or with someone",
    "reason_for_visit": "String with reason for visit"
  }},
  "history": {{
    "presenting_complaints": "String with history of presenting complaints WITH ALL DATES preserved",
    "ideas_concerns_expectations": "String with patient's ideas, concerns, and expectations",
    "red_flag_symptoms": "String indicating presence or absence of red flag symptoms",
    "risk_factors": "String with relevant risk factors",
    "past_medical_history": "String with past medical/surgical history",
    "medications": "String with medications WITH EXACT dosages and frequency",
    "allergies": "String with allergies",
    "family_history": "String with relevant family history",
    "social_history": "String with social history details"
  }},
  "examination": {{
    "vital_signs": "String with vital signs WITH EXACT measurements",
    "physical_findings": "String with physical examination findings",
    "investigations": "String with investigations and results"
  }},
  "impression": [
    {{
      "issue": "ONE specific symptom or problem (DO NOT combine multiple symptoms)",
      "diagnosis": "The most likely diagnosis for THIS SPECIFIC issue",
      "differential_diagnosis": "Alternative diagnoses for THIS SPECIFIC issue"
    }}
  ],
  "plan": {{
    "investigations": "String with planned investigations",
    "treatment": "String with treatment plan WITH EXACT medication details",
    "referrals": "String with referrals",
    "follow_up": "String with follow-up plan WITH timeframe",
    "safety_netting": "String with safety netting advice"
  }},
  "consultation_date": "If no date is mentioned in the conversation, use the current date in the format DD Month YYYY (e.g., '4 April 2025')",
  "patient_name": "String with patient name if mentioned"
}}

IMPORTANT: 
- Your response MUST be a valid JSON object exactly matching this schema
- Use "Not documented" for any field without information in the conversation
- Do not invent information not present in the conversation
- Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
- Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
- Create complete documentation that would meet professional medical standards
- If no consultation date is mentioned in the conversation, generate the current date in the format DD Month YYYY (e.g., '4 April 2025')
- REMEMBER: Create SEPARATE impression items for EACH distinct symptom or issue
"""
        # Similar detailed instructions would be added for other template types
        
        elif template_type == "clinical_report":
            # Clinical Report Instructions - Updated to emphasize preservation
            system_message = f"""You are a clinical documentation expert specializing in comprehensive clinical reports.
{preservation_instructions}
{grammar_instructions}
Format your response as a valid JSON object according to the clinical report schema.
...existing schema here...
"""
        # Continue with other template types
        elif template_type == "psychology_session_notes":
            system_message = f"""You are a clinical psychologist documentation expert specializing in professional session notes.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a clinical psychologist would write - using professional terminology and appropriate clinical language
        2. Be CONCISE and DIRECT in your documentation
        3. Use "Client" instead of "Patient" throughout the document
        4. Use BULLET POINTS for detailed information in each section
        5. Only include information explicitly mentioned in the transcript
        6. Always preserve all dates, therapeutic techniques, and clinical observations EXACTLY as stated
        7. When information for a section is not available, DO NOT include placeholder text - simply leave that section blank
        8. Do not invent or hallucinate any information not present in the conversation

        Format your response as a valid JSON object according to this schema:
        {{
        "session_tasks_review": {{
            "practice_skills": ["Array of details about client's practice of skills/strategies from last session"],
            "task_effectiveness": ["Array of details about completion and effectiveness of tasks"],
            "challenges": ["Array of challenges faced by client in completing tasks"]
        }},
        "current_presentation": {{
            "current_symptoms": ["Array of client's current presentation, symptoms and new issues"],
            "changes": ["Array of changes in symptoms or behaviors since last session"]
        }},
        "session_content": {{
            "issues_raised": ["Array of issues raised by client"],
            "discussions": ["Array of details of relevant discussions during session"],
            "therapy_goals": ["Array of therapy goals/objectives discussed"],
            "progress": ["Array of progress achieved towards therapy goals"],
            "main_topics": ["Array of main topics, insights and client's responses"]
        }},
        "intervention": {{
            "techniques": ["Array of specific therapeutic techniques and interventions used"],
            "strategies": ["Array of specific techniques or strategies and client engagement"]
        }},
        "treatment_progress": {{
            "setbacks": ["Array of setbacks, barriers or obstacles for each therapy goal"],
            "satisfaction": ["Array of client's comments on treatment satisfaction"]
        }},
        "risk_assessment": {{
            "suicidal_ideation": "String detailing any history of suicidal ideation, attempts, plans",
            "homicidal_ideation": "String describing any homicidal ideation",
            "self_harm": "String detailing any history of self-harm",
            "violence": "String describing any incidents of violence or aggression",
            "management_plan": ["Array of strategies to manage risks if applicable"]
        }},
        "mental_status": {{
            "appearance": "String describing client's clothing, hygiene, physical characteristics",
            "behaviour": "String describing client's activity level and interactions",
            "speech": "String noting rate, volume, tone, clarity of speech",
            "mood": "String recording client's self-described emotional state",
            "affect": "String describing range and appropriateness of emotional response",
            "thoughts": "String assessing thought process and content",
            "perceptions": "String noting any reported hallucinations or sensory misinterpretations",
            "cognition": "String describing memory, orientation, concentration",
            "insight": "String describing client's understanding of their condition",
            "judgment": "String describing decision-making ability"
        }},
        "out_of_session_tasks": ["Array of tasks assigned before next session and reasons"],
        "next_session": {{
            "date": "String mentioning date and time of next session",
            "plan": ["Array of topics to address and planned interventions"]
        }}
    }}
    IMPORTANT: 
    - Your response MUST be a valid JSON object exactly matching this schema
    - Only include information explicitly mentioned in the transcript
    - If information for a field is not mentioned, provide an empty array or empty string
    - Do not invent information not present in the conversation
    - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
    - Use "Client" instead of "Patient" throughout
    - Use professional clinical psychology terminology
    """

        elif template_type == "pathology_note":
            system_message = f"""You are a medical documentation expert specializing in professional pathology and therapy notes.
        {preservation_instructions}
        {grammar_instructions}
        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a medical professional would write - using professional medical terminology and standard medical abbreviations
        2. Be CONCISE and DIRECT in your documentation
        3. Focus on capturing critical clinical details using precise medical language
        4. Never miss dates, medication dosages, or measurements - preserve them EXACTLY as stated
        5. Format the note exactly according to the structure provided
        6. Only include information explicitly mentioned in the transcript
        7. When information is not present in the transcript, do not include placeholder text - simply leave that section blank

        Format your response as a valid JSON object according to this schema:
        {{
        "therapy_attendance": {{
            "current_issues": "String describing current issues, reasons for visit, discussion topics, presenting complaints",
            "past_medical_history": "String describing past medical history, previous surgeries",
            "medications": "String listing medications and herbal supplements WITH EXACT dosages",
            "social_history": "String describing social history",
            "allergies": "String listing allergies"
        }},
        "objective": {{
            "examination_findings": "String describing objective findings from examination",
            "diagnostic_tests": "String mentioning relevant diagnostic tests and results"
        }},
        "reports": "String summarizing relevant reports and findings",
        "therapy": {{
            "current_therapy": "String describing current therapy or interventions",
            "therapy_changes": "String mentioning any changes to therapy or interventions"
        }},
        "outcome": "String describing the outcome of the therapy or interventions",
        "plan": {{
            "future_plan": "String outlining the plan for future therapy or interventions",
            "followup": "String mentioning any follow-up appointments or referrals"
        }}
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Use "Not documented" for any field without information in the conversation
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        - Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
        - Create complete documentation that would meet professional medical standards
        """
        
        elif template_type == "progress_note":
            system_message = f"""You are a medical documentation expert specializing in creating detailed and professional progress notes.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a doctor would write - using professional medical terminology
        2. Format MOST sections as FULL PARAGRAPHS with complete sentences (not bullet points)
        3. Only the Plan and Recommendations section should use numbered bullet points
        4. Preserve all clinical details, dates, medication names, and dosages exactly as stated
        5. Be concise yet comprehensive, capturing all relevant clinical information
        6. Use formal medical writing style throughout
        7. Never mention when information is missing - simply leave that section brief or empty
        8. When information is not present in the transcript, DO NOT include placeholder text

        Format your response as a valid JSON object according to this schema:
        {{
        "clinic_info": {{
            "name": "Clinic name if mentioned, otherwise empty string",
            "address_line1": "Address line 1 if mentioned, otherwise empty string",
            "address_line2": "Address line 2 if mentioned, otherwise empty string",
            "phone": "Phone number if mentioned, otherwise empty string",
            "fax": "Fax number if mentioned, otherwise empty string"
        }},
        "practitioner": {{
            "name": "Practitioner's full name if mentioned",
            "title": "Practitioner's title if mentioned"
        }},
        "patient": {{
            "surname": "Patient's last name if mentioned",
            "firstname": "Patient's first name if mentioned",
            "dob": "Patient's date of birth if mentioned (format as DD/MM/YYYY)"
        }},
        "note_date": "Date of note if mentioned, otherwise use current date",
        "introduction": "Full paragraph introducing the patient, including age, marital status, living situation",
        "history": "Full paragraph describing patient's relevant medical history, chronic conditions, treatments",
        "presentation": "Full paragraph describing patient's appearance, demeanor, and who they attended with",
        "mood_mental_state": "Full paragraph describing patient's mood, mental state, thoughts of harm, paranoia, hallucinations",
        "social_functional": "Full paragraph describing social relationships, daily functioning, support systems",
        "physical_health": "Full paragraph describing physical health issues and management",
        "plan": ["Array of specific recommendations, medications, follow-ups as numbered points"],
        "closing": "Final paragraph with any concluding advice or recommendations"
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string 
        - Write most sections as FULL PARAGRAPHS with complete sentences
        - Only the "plan" section should be formatted as an array of bullet points
        - Do not invent information not present in the conversation
        - Use professional medical terminology and maintain formal tone
        """
        
        elif template_type == "meeting_minutes":
            system_message = f"""You are a professional medical scribe specialized in creating concise and accurate meeting minutes from transcribed conversations.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Only include information explicitly mentioned in the transcript. Do not invent or assume details.
        2. Use professional language appropriate for medical settings.
        3. Be concise and clear in your documentation.
        4. Format information in bullet points where appropriate.
        5. If information for a specific section is not present in the transcript, indicate it as "Not documented" or similar appropriate phrasing.
        6. For date and time of the meeting, if not explicitly mentioned, use the current date and time.
        7. Ensure all action items clearly state both the action and the responsible party when available.
        8. Maintain a formal tone throughout the document.
        9. Focus only on information relevant to the meeting - do not include extraneous details.
        10. Ensure grammar and punctuation are perfect throughout.
        11. NEVER refer to participants as "Speaker 0" or "Speaker 1" - instead, use their roles, titles, or names if mentioned, or simply present the information without attributing to specific speakers.
        12. Write discussion points, decisions, and action items in a neutral, professional tone without speaker attribution.

        Format your response as a valid JSON object according to this schema:
        {{
        "date": "Meeting date if mentioned, otherwise empty string",
        "time": "Meeting time if mentioned, otherwise empty string",
        "location": "Meeting location if mentioned, otherwise empty string",
        "attendees": ["Array of attendees with proper names and titles if mentioned - NEVER use Speaker X labels"],
        "agenda_items": ["Array of agenda items if mentioned"],
        "discussion_points": ["Array of detailed discussion points WITHOUT speaker attribution (e.g., 'Patient reported feeling...' not 'Speaker 1 reported feeling...')"],
        "decisions_made": ["Array of decisions made during the meeting WITHOUT speaker attribution"],
        "action_items": ["Array of action items with responsible parties WITHOUT speaker attribution"],
        "next_meeting": {{
            "date": "Date of next meeting if mentioned",
            "time": "Time of next meeting if mentioned",
            "location": "Location of next meeting if mentioned"
        }}
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string or empty array
        - Do not invent information not present in the conversation
        - NEVER use 'Speaker 0' or 'Speaker 1' labels - use professional clinical language instead
        - For discussion points about patients, use terms like "Patient reported..." or "It was noted that the patient..."
        - For decisions or actions by medical professionals, use terms like "Decision to start medication..." or "Clinician recommended..."
        - Be DIRECT and CONCISE in all documentation while preserving accuracy
        - Use professional terminology appropriate for medical settings
        """
    
        elif template_type == "followup_note":
            system_message = f"""You are a clinical documentation specialist focused on creating concise, accurate follow-up notes for mental health practitioners.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Create a concise, well-structured follow-up note using bullet points
        2. Use professional medical terminology appropriate for clinical documentation
        3. Be direct and specific when describing symptoms, observations, and plans
        4. Document only information explicitly mentioned in the transcript
        5. Format each section with clear bullet points
        6. If information for a section is not available, indicate with "Not documented" or similar phrasing
        7. Use objective, clinical language throughout
        8. For date, use the date mentioned in the transcript or the current date if none is mentioned

        Format your response as a valid JSON object according to this schema:
        {{
        "date": "Date of the follow-up if mentioned, otherwise say 'Not documented'",
        "presenting_complaints": [
            "Symptom descriptions, medication adherence, concentration levels as mentioned"
        ],
        "mental_status": {{
            "appearance": "Description of patient's appearance",
            "behavior": "Description of patient's behavior",
            "speech": "Description of patient's speech pattern",
            "mood": "Patient's self-reported mood",
            "affect": "Clinician's observation of patient's affect",
            "thoughts": "Description of thought content and process",
            "perceptions": "Any perceptual disturbances noted",
            "cognition": "Assessment of cognitive functioning",
            "insight": "Assessment of patient's insight",
            "judgment": "Assessment of patient's judgment"
        }},
        "risk_assessment": "Assessment of suicidality and homicidality",
        "diagnosis": [
            "List of diagnoses mentioned"
        ],
        "treatment_plan": "Medication plan, follow-up interval, upcoming tests/procedures",
        "safety_plan": "Safety recommendations",
        "additional_notes": "Any other relevant information"
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide "Not documented" or "None"
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving accuracy
        - Use professional terminology appropriate for mental health settings
        """
        
        elif template_type == "detailed_soap_note":
                    system_message = f"""You are a medical documentation expert specializing in detailed SOAP notes. 
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Be CONCISE and DIRECT in your documentation (e.g., "Lost 2kg in 3 weeks" NOT "The patient reported that they have lost two kilograms over the past three weeks")
        2. Use medical shorthand where appropriate (e.g., "BP 120/80" instead of "Blood pressure is 120/80")
        3. Format all information in BULLET POINT style
        4. Focus on capturing critical details using the fewest words necessary
        5. Use medical terminology that healthcare professionals would use
        6. NEVER create or invent information not explicitly mentioned in the transcript

        When formatting the provided medical conversation, structure it according to the SOAP format:

        Format your response as a valid JSON object according to this schema:
        {{
        "subjective": {{
            "current_issues": "CONCISE string describing presenting complaints WITH EXACT TIMELINE",
            "past_medical_history": "CONCISE string with past medical conditions and surgeries",
            "medications": "CONCISE list of ALL medications with EXACT dosages and frequency",
            "social_history": "CONCISE string with lifestyle factors, occupation, and habits",
            "allergies": "CONCISE string with any allergies and reactions"
        }},
        "objective": {{
            "vital_signs": "CONCISE string with ALL vital sign measurements EXACTLY as stated",
            "physical_examination": "CONCISE string with physical examination findings by body system",
            "laboratory_results": "CONCISE string with lab test results",
            "imaging_results": "CONCISE string with imaging test results",
            "other_diagnostics": "CONCISE string with other diagnostic test results"
        }},
        "assessment": {{
            "diagnosis": "CONCISE string with diagnoses or differential diagnoses",
            "clinical_impression": "CONCISE string with clinical impression or case summary"
        }},
        "plan": {{
            "treatment": "CONCISE string with treatment plan INCLUDING ALL medications, dosages, and frequencies",
            "patient_education": "CONCISE string with patient education and counseling points",
            "referrals": "CONCISE string with referrals to other providers or specialists",
            "additional_instructions": "CONCISE string with any additional instructions or recommendations"
        }}
        }}

        IMPORTANT: 
        - Your response MUST be a valid JSON object exactly matching this schema
        - Use "Not documented" for any field without information in the conversation
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        - Always preserve ALL dates, medication dosages, and measurements EXACTLY as stated
        """

        elif template_type == "case_formulation":
                system_message = """You are a mental health professional tasked with creating a concise case formulation report following the 4Ps schema (Predisposing, Precipitating, Perpetuating, and Protective factors). Based on the provided transcript of a clinical session, extract and organize relevant information into the following sections.

        Your response MUST be a valid JSON object with EXACTLY these keys:
        - client_goals: The client's expressed goals and aspirations
        - presenting_problems: The main issues the client is experiencing (string or array of strings)
        - predisposing_factors: Factors that may have contributed to the development of the presenting issues (string or array of strings)
        - precipitating_factors: Events or circumstances that triggered the onset of the presenting issues (string or array of strings)
        - perpetuating_factors: Factors that are maintaining or exacerbating the presenting issues (string or array of strings)
        - protective_factors: Factors that are helping the client cope with or mitigate the presenting issues (string or array of strings)
        - problem_list: List of identified problems/issues (string or array of strings)
        - treatment_goals: Specific goals to be achieved through treatment (string or array of strings)
        - case_formulation: Comprehensive explanation of the client's issues, incorporating biopsychosocial factors
        - treatment_mode: Description of the treatment modalities and interventions planned or in use (string or array of strings)

        Example response structure:
        {
        "client_goals": "The client aims to improve their mental health and manage anxiety",
        "presenting_problems": ["Depression", "Anxiety", "Panic attacks"],
        "predisposing_factors": ["Family history of mental health issues"],
        "precipitating_factors": ["Recent job stress"],
        "perpetuating_factors": ["Avoidance behaviors"],
        "protective_factors": ["Supportive spouse"],
        "problem_list": ["Severe anxiety", "Frequent panic attacks"],
        "treatment_goals": ["Reduce anxiety", "Improve coping skills"],
        "case_formulation": "The client presents with anxiety likely influenced by...",
        "treatment_mode": ["Cognitive-behavioral therapy", "Medication management"]
        }

        Use professional clinical language and be concise yet comprehensive. If information for any section is not available in the transcript, use the value "Not documented" for that field."""
                schema = CASE_FORMULATION_SCHEMA

        elif template_type == "discharge_summary":
            system_message = f"""You are a mental health professional documentation expert specializing in comprehensive discharge summaries.
        {preservation_instructions}
        {grammar_instructions}

        CRITICAL STYLE INSTRUCTIONS:
        1. Write as a professional clinician would write - using appropriate clinical terminology and professional language
        2. Be CONCISE and DIRECT in your documentation
        3. Always use "Client" instead of "Patient" 
        4. Focus on capturing critical clinical details using precise professional language
        5. Preserve all dates, diagnoses, and clinical observations EXACTLY as stated
        6. Format the summary exactly according to the structure provided
        7. Only include information explicitly mentioned in the transcript
        8. Use bullet points for lists of goals, progress, strengths, challenges, recommendations, etc.

        Format your response as a valid JSON object according to this schema:
        {{
        "client": {{
            "name": "Client's full name if mentioned",
            "dob": "Client's date of birth if mentioned",
            "discharge_date": "Date of discharge if mentioned"
        }},
        "referral": {{
            "source": "Name and contact details of referring individual/agency",
            "reason": "Brief summary of the reason for referral"
        }},
        "presenting_issues": ["Array of presenting issues"],
        "diagnosis": ["Array of diagnoses"],
        "treatment_summary": {{
            "duration": "Start date and end date of therapy",
            "sessions": "Total number of sessions attended",
            "therapy_type": "Type of therapy provided (CBT, ACT, etc.)",
            "goals": ["Array of therapeutic goals"],
            "description": "Description of treatment provided",
            "medications": "Any medications prescribed"
        }},
        "progress": {{
            "overall": "Client's overall progress and response to treatment",
            "goal_progress": ["Array of progress descriptions for each goal"]
        }},
        "clinical_observations": {{
            "engagement": "Client's participation and engagement in therapy",
            "strengths": ["Array of client's strengths identified during treatment"],
            "challenges": ["Array of client's challenges identified during treatment"]
        }},
        "risk_assessment": "Description of risk factors or concerns identified at discharge",
        "outcome": {{
            "current_status": "Summary of client's mental health status at discharge",
            "remaining_issues": "Any ongoing issues not fully resolved",
            "client_perspective": "Client's view of their progress and outcomes",
            "therapist_assessment": "Professional assessment of the outcome"
        }},
        "discharge_reason": {{
            "reason": "Reason for discharge",
            "client_understanding": "Client's understanding and agreement with discharge plan"
        }},
        "discharge_plan": "Outline of discharge plan including follow-up appointments",
        "recommendations": {{
            "overall": "Overall recommendations identified",
            "followup": ["Array of follow-up care recommendations"],
            "self_care": ["Array of self-care strategies"],
            "crisis_plan": "Instructions for handling potential crises",
            "support_systems": "Encouragement to engage with personal support"
        }},
        "additional_notes": "Any additional notes relevant to discharge",
        "final_note": "Therapist's closing remarks",
        "clinician": {{
            "name": "Clinician's full name",
            "date": "Date of document completion"
        }},
        "attachments": ["Array of attached documents"]
        }}

        IMPORTANT:
        - Your response MUST be a valid JSON object exactly matching this schema
        - Only include information explicitly mentioned in the transcript
        - If information for a field is not mentioned, provide an empty string or empty array
        - Do not invent information not present in the conversation
        - Be DIRECT and CONCISE in all documentation while preserving clinical accuracy
        - Always use "Client" instead of "Patient" throughout
        """
        
        elif template_type == "new_soap_note":
            system_message = f"""You are an expert medical documentation specialist with extensive experience in clinical documentation. Your task is to create precise, professional SOAP notes from medical conversations that matches this schema {NEW_SOAP_SCHEMA} following these guidelines:

        {preservation_instructions}
        {grammar_instructions}


        CORE PRINCIPLES:
        - Extract information ONLY from the provided transcript/notes - never fabricate or assume details
        - Use professional medical terminology and standard medical abbreviations
        - Maintain clinical objectivity and conciseness
        - Structure information logically within each section
        - Leave sections blank if information is not explicitly mentioned
        - Format vital signs using standard medical notation (e.g., "BP: 120/80 mmHg")
        - DO not include the past medical history in the subjective section.
        - Never add any information in the report that is not present in the transcript.
        - Give intelligence to the doctor and make the report more informative and document all findings effectively.
        - Dont include any information in the plan section that is not present in the transcript.
        SECTION-SPECIFIC GUIDELINES:

        SUBJECTIVE (S):
        - Document chief complaints with precise onset, duration, and character
        - Note modifying factors and temporal patterns
        - Include relevant self-management attempts and their efficacy
        - Document functional impact on ADLs/occupation
        - List associated symptoms systematically
        - For multiple complaints, enumerate each distinctly
        - Use medical terminology (e.g., "dyspnea on exertion" rather than "shortness of breath with activity")
        - Format subjective information in complete, professional sentences, with each point on a new line
        
        Example format:
        . 46-year-old male with PMHx of mild asthma and T2DM presents with progressive respiratory symptoms over 1 week.
        - Symptoms initially attributed to URI but have worsened over past 6 days.
        - Primary symptoms include persistent cough productive of yellow-green sputum, dyspnea, and chest tightness.
        - Associated symptoms include intermittent low-grade fever.
        - Dyspnea significant with minimal exertion, leading to fatigue after walking few steps.
        - Reports wheezing and chest heaviness; denies pleuritic chest pain.
        - Recent exposure to URI from son last week; denies recent travel.
        
        Key formatting principles:
        1. Begin with patient demographics 
        2. Present symptoms in chronological order
        3. Use precise medical terminology
        4. Document pertinent positives and negatives
        5. Include relevant exposures and risk factors
        6. Maintain professional, concise language throughout
        7. Past medical history should not be included in the subjective section as it has its own section.


        PAST MEDICAL HISTORY (PMedHx):
        - List relevant medical conditions, surgeries, and treatments
        - Document pertinent negative findings
        - Include immunization status if mentioned
        - Note relevant investigations and their outcomes

        SOCIAL HISTORY (SocHx):
        - Document relevant lifestyle factors
        - Include occupational exposures if pertinent
        - Note substance use/habits
        - Document living situation if relevant

        FAMILY HISTORY (FHx):
        - Document relevant hereditary conditions
        - Include age of onset if mentioned
        - Note pertinent negative findings

        OBJECTIVE (O):
        - Begin with general appearance (use "NAD" if appropriate)
        - Format vital signs precisely:
        BP: [value] mmHg
        HR: [value] bpm
        Wt: [value] kg
        T: [value]°C
        O2: [value]%
        Ht: [value] cm
        - Use standard normal exam phrases:
        "N S1 and S2, no murmurs or extra beats"
        "Resp: Chest clear, no decr breath sounds"
        "No distension, BS+, soft, non-tender to palpation and percussion. No organomegaly"
        - For psychiatric evaluations, include:
        Appearance, speech, affect, perception, thought form/content, insight/judgment, cognition

        ASSESSMENT/PLAN (A/P):
        For each issue:
        1. State the problem/condition concisely
        2. Document assessment with likely diagnosis
        3. List differential diagnoses in order of probability
        4. Specify investigations with clear rationale
        5. Detail treatment plan with specific interventions
        6. Note relevant referrals and follow-up plans
        7. Include the follow up plan in the plan section.
        8. Dont include random investigations in the plan section, only include the investigations that are relevant to the issue and somewhat related to the discussion in the transcript.
        9. For each issue, take separate Assessment, Differential diagnosis, Investigations planned, Treatment planned, Relevant referrals, Follow up plan, dont add them in the same issue.
        10. Format assessment entries efficiently - avoid redundancy:
        INCORRECT:
        Issue: Acute bronchitis complicated by asthma and diabetes
        Assessment: Likely diagnosis is acute bronchitis complicated by asthma and diabetes

        CORRECT:
        Issue: Acute bronchitis
        Assessment: Complicated by underlying asthma and diabetes. Patient presents with...

        FORMAT EACH ISSUE IN THE PLAN AS FOLLOWS:
        1. [Problem/Condition]
           Assessment: [Likely diagnosis]
           Differential diagnosis: [List alternatives in order of probability]
           Investigations planned: [List with rationale]
           Treatment planned: [Specific interventions with dosages]
           Relevant referrals: [Referrals with timeframes]
           Follow up plan: [Follow up plan with timeframes]
           
        Example format:
        1. Asthma exacerbation
           Assessment: Likely diagnosis is asthma exacerbation
           Differential diagnosis: COPD, bronchitis
           Investigations planned: Spirometry, chest X-ray
           Treatment planned: Prescribe inhaled corticosteroids and bronchodilators
           Relevant referrals: Referral to pulmonologist for further evaluation
           Follow up plan: Follow up in 2 weeks

        2. Fatigue
           Assessment: Likely diagnosis is related to asthma exacerbation
           Differential diagnosis: Anemia, sleep apnea
           Investigations planned: Complete blood count, sleep study
           Treatment planned: Address underlying asthma exacerbation
           Relevant referrals: None at this time
           Follow up plan: Follow up in 4-5 days if not better

        FORMATTING RULES:
        - Use numerical ordering for multiple issues
        - Maintain consistent indentation
        - Use medical abbreviations appropriately
        - Employ subheadings in Assessment/Plan sections
        - Keep entries concise but complete

        CRITICAL NOTES:
        1. Never fabricate or assume information not present in the source material
        2. Leave sections blank rather than noting "not mentioned" or "unknown"
        3. Maintain professional medical tone throughout
        4. Focus on clinically relevant information
        5. Use standard medical terminology and phrasing
        6. Recognize and document pertinent positive and negative findings
        7. Interpret examination findings accurately based on clinical context
        8. Structure differential diagnoses in order of clinical probability
        9. Ensure treatment plans align with current medical standards
        10. Maintain logical flow and clinical coherence throughout the note
        11. CRITICAL FINDINGS: Document ALL abnormal vital signs, concerning symptoms, or red flags that require immediate clinical attention (e.g., "BP 180/110", "SpO2 88%", "Acute chest pain")
        12. VITAL SIGNS: Always document complete set of vital signs with exact values:
            - Blood Pressure (BP)
            - Heart Rate (HR)
            - Respiratory Rate (RR)
            - Temperature (T)
            - Oxygen Saturation (SpO2)
            - Weight (Wt)
            - Height (Ht)
            - BMI if available
        13. DIAGNOSTIC REASONING: For each diagnosis, document:
            - Supporting clinical findings
            - Relevant test results
            - Clinical reasoning for diagnosis
            - Severity assessment
            - Disease staging if applicable
        14. DIFFERENTIAL DIAGNOSIS: For each primary diagnosis, include:
            - At least 2-3 alternative diagnoses
            - Key distinguishing features
            - Plan for ruling out each alternative
        15. TREATMENT PLAN: Document comprehensively:
            - Medications with exact dosing, frequency, duration
            - Non-pharmacological interventions
            - Patient education points
            - Treatment goals and expected outcomes
            - Monitoring parameters
        16. PROGNOSIS: Include specific details about:
            - Expected course of condition
            - Timeline for improvement
            - Potential complications
            - Risk factors for poor outcomes
            - Quality of life impact
        17. FOLLOW-UP PLAN: Specify:
            - Next appointment timing
            - Monitoring requirements
            - Trigger points for earlier review
            - Specialty referrals with timeframes
            - Care coordination plans
        18. SELF-CARE PLAN: Document specific instructions for:
            - Lifestyle modifications
            - Diet and exercise recommendations
            - Home monitoring requirements
            - Warning signs to watch for
            - Emergency action plans
        19. STRESS MANAGEMENT: Include detailed strategies for:
            - Stress reduction techniques
            - Coping mechanisms
            - Support resources
            - Crisis management plan
            - Mental health support options
        20. INVESTIGATIONS: Document all tests with:
            - Specific test names
            - Timing requirements
            - Preparation instructions
            - Expected result timeframes
            - Follow-up plan for results
        21. Analyse the examination doctor is doing like open ur mouth, listen to ur heart, listen to ur lungs, etc. and document it in the note with what the examination is called and what were the findings.

        Your output should reflect the highest standards of medical documentation, demonstrating clinical expertise while maintaining accuracy and relevance to the presented case. 
        Always include all vital signs, immunization and family history, and provide more detailed diagnostic reasoning for precision.
        ["The patient's temperature is 106°F, and the patient's oxygen saturation level is slightly low (94%), which are the critical findings that requires immediate attention."]"""
                
        # Make the API request to GPT - Remove the response_format parameter which is causing the error
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": f"Here is a medical conversation. Please format it into a structured {template_type}. YOUR RESPONSE MUST BE VALID JSON:\n\n{conversation_text}"}
            ],
            temperature=0.1  # Lower temperature for more consistent outputs
            # Removed the response_format parameter that was causing the error
        )
        
        # Extract the response and validate JSON
        gpt_response = response.choices[0].message.content
        
        # Validate that the response is proper JSON
        try:
            json.loads(gpt_response)
        except json.JSONDecodeError:
            # If not valid JSON, log the error and return error message
            error_msg = f"GPT did not return valid JSON: {gpt_response[:100]}..."
            error_logger.error(error_msg)
            return f"Error: {error_msg}"
        
        # Log success and return response
        main_logger.info(f"[OP-{operation_id}] GPT response generated successfully for template: {template_type}")
        return gpt_response
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error generating GPT response: {str(e)}", exc_info=True)
        return f"Error generating GPT response: {str(e)}"

@log_execution_time
async def format_report(gpt_response, template_type):
    """
    Format a report based on the template type.
    
    Args:
        gpt_response: JSON string containing the GPT formatted response
        template_type: Type of report template to use
        
    Returns:
        String containing the formatted report or error message
    """
    try:
        # Parse the GPT response
        data = json.loads(gpt_response)  # Ensure gpt_response is parsed as JSON
        
        # Add grammar validation instruction to the data
        if isinstance(data, dict):
            data["_grammar_instruction"] = "Ensure all text follows standard US English grammar rules with proper spelling, punctuation, and capitalization."
        
        # Format based on template type
        if template_type == "clinical_report":
            return await format_clinical_report(data)
        elif template_type == "soap_note":
            return await format_soap_note(data)
        elif template_type == "new_soap_note":
            return await format_new_soap(data)
        elif template_type == "progress_note":
            return await format_progress_note(data)
        elif template_type == "mental_health_appointment":
            return await format_mental_health_note(data)
        elif template_type == "cardiology_letter":
            return await format_cardiology_letter(data)
        elif template_type == "followup_note":
            return await format_followup_note(data)
        elif template_type == "meeting_minutes":
            return await format_meeting_minutes(data)
        elif template_type == "referral_letter":
            return await format_referral_letter(data)
        elif template_type == "detailed_dietician_initial_assessment":
            return await format_dietician_assessment(data)
        elif template_type == "psychology_session_notes":
            return await format_psychology_session_notes(data)
        elif template_type == "pathology_note":
            return await format_pathology(data)
        elif template_type == "consult_note":
            return await format_consult_note(data)
        elif template_type == "discharge_summary":
            return await format_discharge_summary(data)
        elif template_type == "case_formulation":
            return await format_case_formulation(data)
        elif template_type == "detailed_soap_note":
            return await format_detailed_soap_note(data)
        else:
            return f"Error: Unsupported template type '{template_type}'"
    except json.JSONDecodeError:
        error_logger.error(f"Invalid JSON in GPT response: {gpt_response}")
        return f"Error: Invalid JSON format in GPT response"
    except Exception as e:
        error_logger.error(f"Error formatting report: {str(e)}", exc_info=True)
        return f"Error formatting report: {str(e)}"

@log_execution_time
async def save_to_aws(transcription, gpt_response, formatted_report, template_type, audio_data=None, status="completed"):
    """
    Save all data to AWS storage services.
    
    This integrated method saves audio to S3 and metadata to DynamoDB.
    It includes the transcription, GPT response, and formatted report.
    
    Args:
        transcription: Dictionary containing the transcribed conversation
        gpt_response: JSON string containing structured report data
        formatted_report: Formatted string containing the human-readable report
        template_type: Type of report (clinical_report, soap_note, etc.)
        audio_data: Binary audio data to save (optional)
        status: Processing status (completed, partial, or failed)
    
    Returns:
        Dictionary containing transcript_id and report_id if successful, False otherwise
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        db_logger.info(f"[OP-{operation_id}] Starting save_to_aws operation")
        
        # Save audio if provided
        audio_info = None
        if audio_data:
            db_logger.info(f"[OP-{operation_id}] Saving audio data to S3")
            audio_info = await save_audio_to_s3(audio_data)
            if not audio_info:
                db_logger.error(f"[OP-{operation_id}] Failed to save audio to S3")
                return False
            db_logger.info(f"[OP-{operation_id}] Audio saved to S3: {audio_info['s3_path']}")
        
        # Save transcript
        db_logger.info(f"[OP-{operation_id}] Saving transcript to DynamoDB with status: {status}")
        transcript_id = await save_transcript_to_dynamodb(transcription, audio_info, status)
        
        if not transcript_id:
            db_logger.error(f"[OP-{operation_id}] Failed to save transcript to DynamoDB")
            return False
            
        db_logger.info(f"[OP-{operation_id}] Transcript saved with ID: {transcript_id}")
            
        # Save report if available (might be None if processing failed)
        report_id = None
        if gpt_response and formatted_report:
            db_logger.info(f"[OP-{operation_id}] Saving report to DynamoDB for transcript ID: {transcript_id}")
            report_id = await save_report_to_dynamodb(
                transcript_id,
                gpt_response,
                formatted_report,
                template_type,
                status
            )
            
            if not report_id:
                db_logger.error(f"[OP-{operation_id}] Failed to save report to DynamoDB")
                return {"transcript_id": transcript_id, "report_id": None}
                
            db_logger.info(f"[OP-{operation_id}] Report saved with ID: {report_id}")
        
        return {
            "transcript_id": transcript_id,
            "report_id": report_id
        }
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving to AWS: {str(e)}", exc_info=True)
        return False

async def receive_from_client(websocket, deepgram_socket):
    """
    Receive audio data from client and forward to Deepgram.
    
    This function handles the WebSocket communication from client to Deepgram,
    forwarding audio data and processing control messages.
    
    Args:
        websocket: The client WebSocket connection
        deepgram_socket: The Deepgram WebSocket connection
    """
    try:
        while True:
            # Receive message from client - could be audio data or a control message
            message = await websocket.receive()
            
            # Check if this is a text message (control message)
            if "text" in message:
                control_message = json.loads(message["text"])
                if control_message.get("type") == "end_audio":
                    # Client indicates they've finished sending audio
                    break
                continue
                
            # Otherwise it's binary audio data
            data = message.get("bytes")
            if not data:
                continue
                
            # Forward the audio data to Deepgram
            await deepgram_socket.send(data)
            
    except WebSocketDisconnect:
        main_logger.info("Client disconnected during audio streaming")
    except Exception as e:
        error_logger.error(f"Error receiving from client: {str(e)}", exc_info=True)
        raise

async def receive_from_deepgram(deepgram_socket, client_id, session_metadata):
    """
    Receive transcription results from Deepgram and send to client.
    
    This function processes the real-time transcription results from Deepgram,
    structures them with speaker information, and sends them to the client.
    
    Args:
        deepgram_socket: The Deepgram WebSocket connection
        client_id: Unique identifier for the client
        session_metadata: Dictionary containing session metadata
    """
    try:
        mode = session_metadata.get("mode", "word-to-word")
        session_data = manager.get_session_data(client_id)
        
        while True:
            # Receive transcription results from Deepgram
            response = await deepgram_socket.recv()
            response_json = json.loads(response)
            
            # Check if this is the final response for a segment
            is_final = response_json.get("is_final", False)
            
            if "channel" in response_json:
                channel = response_json["channel"]
                alternatives = channel.get("alternatives", [])
                
                if alternatives:
                    transcript = alternatives[0].get("transcript", "")
                    
                    if transcript.strip():
                        # Process speaker diarization if available
                        if "words" in alternatives[0]:
                            words = alternatives[0]["words"]
                            speaker_segments = {}
                            
                            for word in words:
                                speaker = word.get("speaker", 0)
                                if speaker not in speaker_segments:
                                    speaker_segments[speaker] = []
                                speaker_segments[speaker].append(word.get("word", ""))
                            
                            # For each speaker, send their portion of the transcript
                            for speaker, words_list in speaker_segments.items():
                                speaker_text = " ".join(words_list)
                                
                                # Add speaker to session metadata
                                session_metadata["speakers"].add(speaker)
                                
                                # Add to transcript if this is a final response
                                if is_final:
                                    transcript_entry = {
                                        "speaker": f"Speaker {speaker}",
                                        "text": speaker_text
                                    }
                                    session_metadata["transcript"].append(transcript_entry)
                                    session_data["complete_transcript"].append(transcript_entry)
                                
                                # Always send to client for word-to-word mode
                                # In AI summary mode, we can still send updates but mark differently
                                transcript_type = "final" if is_final else "interim"
                                await manager.send_message(
                                    json.dumps({
                                        "type": transcript_type,
                                        "speaker": f"Speaker {speaker}",
                                        "text": speaker_text,
                                        "is_final": is_final
                                    }),
                                    client_id
                                )
                        else:
                            # No speaker diarization, just send the transcript
                            transcript_type = "final" if is_final else "interim"
                            await manager.send_message(
                                json.dumps({
                                    "type": transcript_type,
                                    "speaker": "Unknown",
                                    "text": transcript,
                                    "is_final": is_final
                                }),
                                client_id
                            )
                            
                            # Add to transcript if this is a final response
                            if is_final:
                                transcript_entry = {
                                    "speaker": "Unknown",
                                    "text": transcript
                                }
                                session_metadata["transcript"].append(transcript_entry)
                                session_data["complete_transcript"].append(transcript_entry)
            
            # Update session data
            manager.update_session_data(client_id, {
                "complete_transcript": session_data["complete_transcript"]
            })
            
            # Check if we received a close message
            if "type" in response_json and response_json["type"] == "CloseMessage":
                # Mark transcription as complete
                manager.update_session_data(client_id, {
                    "is_transcription_complete": True
                })
                
                # Notify client that transcription is complete
                await manager.send_message(
                    json.dumps({
                        "type": "transcription_complete",
                        "message": "Transcription processing complete."
                    }),
                    client_id
                )
                break
                
    except websockets.exceptions.ConnectionClosed:
        main_logger.info("Deepgram connection closed")
    except Exception as e:
        error_logger.error(f"Error receiving from Deepgram: {str(e)}", exc_info=True)
        raise

# Save audio file to S3
async def save_audio_to_s3(audio_data, filename=None):
    try:
        operation_id = str(uuid.uuid4())[:8]
        if not filename:
            filename = f"audio_{uuid.uuid4()}.wav"
            
        db_logger.info(f"[OP-{operation_id}] Saving audio to S3: {filename}, size: {len(audio_data)} bytes")
        
        # Upload to S3
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=f"audio/{filename}",
            Body=audio_data,
            ContentType="audio/wav"
        )
        
        # Generate a presigned URL for temporary access (optional)
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': f"audio/{filename}"},
            ExpiresIn=3600  # URL valid for 1 hour
        )
        
        db_logger.info(f"[OP-{operation_id}] Audio successfully saved to S3: s3://{S3_BUCKET}/audio/{filename}")
        
        return {
            "filename": filename,
            "s3_path": f"audio/{filename}",
            "presigned_url": presigned_url
        }
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving audio to S3: {str(e)}", exc_info=True)
        return None

# Save transcript data to DynamoDB
async def save_transcript_to_dynamodb(transcript_data, audio_info=None, status="completed"):
    try:
        operation_id = str(uuid.uuid4())[:8]
        transcript_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        db_logger.info(f"[OP-{operation_id}] Saving transcript to DynamoDB, ID: {transcript_id}, status: {status}")
        encrypted_transcription = encrypt_data(json.dumps(transcript_data).encode())
        # Create item for DynamoDB
        item = {
            "id": transcript_id,
            "transcript": encrypted_transcription,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": status
        }
        
        if audio_info:
            item["audio_file"] = json.dumps(audio_info)
        
        # Save to DynamoDB
        table = dynamodb.Table('transcripts')
        table.put_item(Item=item)
        
        db_logger.info(f"[OP-{operation_id}] Transcript successfully saved to DynamoDB: {transcript_id}")
        return transcript_id
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving transcript to DynamoDB: {str(e)}", exc_info=True)
        return None

async def save_summary_to_dynamodb(ai_summary, transcript_id):
    table = dynamodb.Table('summaries')
    summary_id = str(uuid.uuid4())
    encrypted_summary = encrypt_data(ai_summary.encode())
    item = {
        "id": summary_id,
        "transcript_id": transcript_id,
        "summary": encrypted_summary,
        "status": "completed",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    table.put_item(Item=item)
    db_logger.info(f"Updating transcript with ID: {transcript_id}")
    # Update the transcription record with the summary information
    table = dynamodb.Table('transcripts')
    response = table.update_item(
        Key={"id": transcript_id},  # Ensure "id" is the correct key
        UpdateExpression="SET ai_summary = :ai_summary",
        ExpressionAttributeValues={
            ":ai_summary": ai_summary
        },
        ReturnValues="UPDATED_NEW"
    )
    db_logger.info(f"Updating transcript with ID: {transcript_id}")
    return summary_id
    
# Save formatted report to DynamoDB
async def save_report_to_dynamodb(transcript_id, gpt_response, formatted_report, template_type, status="completed"):
    try:
        operation_id = str(uuid.uuid4())[:8]
        report_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        
        db_logger.info(f"[OP-{operation_id}] Saving report to DynamoDB, ID: {report_id}, linked to transcript: {transcript_id}")
        encrypted_gpt_response = encrypt_data(gpt_response.encode())
        

        # Create item for DynamoDB
        item = {
            "id": report_id,
            "transcript_id": transcript_id,
            "gpt_response": encrypted_gpt_response,
            "formatted_report": formatted_report,
            "template_type": template_type,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": status
        }
        
        # Save to DynamoDB
        table = dynamodb.Table('reports')
        table.put_item(Item=item)
        
        db_logger.info(f"[OP-{operation_id}] Report successfully saved to DynamoDB: {report_id}")
        # Update the transcription record with the new report ID
        table = dynamodb.Table('transcripts')
        response = table.update_item(
            Key={"id": transcript_id},
            UpdateExpression="SET report_ids = list_append(if_not_exists(report_ids, :empty_list), :new_report_id)",
            ExpressionAttributeValues={
                ":new_report_id": [report_id],
                ":empty_list": []
            },
            ReturnValues="UPDATED_NEW"
        )
        return report_id
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error saving report to DynamoDB: {str(e)}", exc_info=True)
        return None


@app.get("/list-failed-transcriptions")
async def list_failed_transcriptions():
    """
    List all audio files with failed transcriptions or processing
    """
    try:
        # Query DynamoDB for failed items
        table = dynamodb.Table('transcripts')
        response = table.scan(
            FilterExpression="#status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "failed"}
        )
        
        items = response.get('Items', [])
        
        # Format the response
        failed_items = []
        for item in items:
            audio_info = json.loads(item.get('audio_file', '{}'))
            failed_items.append({
                "id": item.get('id'),
                "created_at": item.get('created_at'),
                "audio_filename": audio_info.get('filename', 'Unknown'),
                "s3_path": audio_info.get('s3_path')
            })
        
        return {"failed_items": failed_items}
    except Exception as e:
        error_logger.error(f"Error listing failed transcriptions: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to list transcriptions: {str(e)}"},
            status_code=500
        )

@app.post("/retry-transcription/{transcript_id}")
@log_execution_time
async def retry_transcription(transcript_id: str):
    """
    Retry transcription for an existing transcript.
    
    Args:
        transcript_id: ID of the transcript to retry
        
    Returns:
        Updated transcription data
    """
    try:
        # Get the transcript from DynamoDB
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
        
        transcript_item = response['Item']
        
        # Get audio file info
        try:
            audio_info = json.loads(transcript_item.get('audio_file', '{}'))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid audio file data format in transcript record"},
                status_code=400
            )
        
        # Check if audio file exists
        s3_path = audio_info.get('s3_path')
        if not s3_path:
            return JSONResponse(
                {"error": "No audio file associated with this transcript"},
                status_code=400
            )
        
        # Download audio file from S3
        main_logger.info(f"Downloading audio file from S3: {s3_path}")
        try:
            s3_response = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_path)
            audio_data = s3_response['Body'].read()
        except Exception as e:
            error_logger.error(f"Error downloading audio file from S3: {str(e)}")
            return JSONResponse(
                {"error": f"Failed to download audio file: {str(e)}"},
                status_code=500
            )
        
        # Retry transcription
        transcription_result = await transcribe_audio_with_diarization(audio_data)
        
        # If transcription fails, return error
        if isinstance(transcription_result, str) and transcription_result.startswith("Error"):
            return JSONResponse(
                {"error": transcription_result},
                status_code=400
            )
        
        # Update the transcript in DynamoDB
        now = datetime.now().isoformat()
        table.update_item(
            Key={"id": transcript_id},
            UpdateExpression="SET transcript = :transcript, updated_at = :updated_at, #status_field = :status",
            ExpressionAttributeNames={
                "#status_field": "status"
            },
            ExpressionAttributeValues={
                ":transcript": json.dumps(transcription_result),
                ":updated_at": now,
                ":status": "completed"
            }
        )
        
        return {
            "success": True,
            "message": "Transcription retried successfully",
            "transcript_id": transcript_id,
            "transcription": transcription_result
        }
        
    except Exception as e:
        error_logger.exception(f"Error retrying transcription: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to retry transcription: {str(e)}"},
            status_code=500
        )

@app.post("/retry-report/{report_id}")
@log_execution_time
async def retry_report(report_id: str, template_type: str = None):
    """
    Retry report generation for an existing report or with a new template type.
    
    Args:
        report_id: ID of the report to retry
        template_type: Optional new template type to use instead of the original
        
    Returns:
        Updated report data
    """
    try:
        # Get the report from DynamoDB
        reports_table = dynamodb.Table('reports')
        response = reports_table.get_item(Key={"id": report_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Report ID {report_id} not found"},
                status_code=404
            )
        
        report_item = response['Item']
        transcript_id = report_item.get('transcript_id')
        original_template = report_item.get('template_type')
        
        # Use provided template type or fall back to original
        if not template_type:
            template_type = original_template
        
        # Get the transcript data
        transcripts_table = dynamodb.Table('transcripts')
        transcript_response = transcripts_table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in transcript_response:
            return JSONResponse(
                {"error": f"Associated transcript ID {transcript_id} not found"},
                status_code=404
            )
        
        transcript_item = transcript_response['Item']
        
        # Parse transcript data
        try:
            transcription = json.loads(transcript_item.get('transcript', '{}'))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid transcript data format"},
                status_code=400
            )
        
        # Validate template type
        valid_templates = ["clinical_report", "new_soap_note","detailed_soap_note","soap_note", "progress_note", "mental_health_appointment", "cardiology_letter", "followup_note", "meeting_minutes","referral_letter","detailed_dietician_initial_assessment","psychology_session_notes", "pathology_note", "consult_note","discharge_summary","case_formulation"]
        if template_type not in valid_templates:
            error_msg = f"Invalid template type '{template_type}'. Must be one of: {', '.join(valid_templates)}"
            return JSONResponse({"error": error_msg}, status_code=400)
        
        # Generate new GPT response
        gpt_response = await generate_gpt_response(transcription, template_type)
        
        if isinstance(gpt_response, str) and gpt_response.startswith("Error"):
            return JSONResponse({"error": gpt_response}, status_code=400)
        
        # Format the report
        formatted_report = await format_report(gpt_response, template_type)
        
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            return JSONResponse({"error": formatted_report}, status_code=400)
        
        # Update the report in DynamoDB
        now = datetime.now().isoformat()
        reports_table.update_item(
            Key={"id": report_id},
            UpdateExpression="SET gpt_response = :gpt_response, formatted_report = :formatted_report, template_type = :template_type, updated_at = :updated_at",
            ExpressionAttributeValues={
                ":gpt_response": gpt_response,
                ":formatted_report": formatted_report,
                ":template_type": template_type,
                ":updated_at": now
            }
        )
        
        return {
            "success": True,
            "message": "Report generation retried successfully",
            "report_id": report_id,
            "template_type": template_type,
            "formatted_report": formatted_report,
            "gpt_response": json.loads(gpt_response)
        }
        
    except Exception as e:
        error_logger.exception(f"Error retrying report generation: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to retry report generation: {str(e)}"},
            status_code=500
        )

@app.post("/generate-summary/{transcript_id}")
@log_execution_time
async def generate_ai_summary(transcript_id: str):
    """
    Generate or regenerate an AI summary for a transcript.
    
    Args:
        transcript_id: ID of the transcript to summarize
        
    Returns:
        AI summary of the transcript
    """
    try:
        # Get the transcript from DynamoDB
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
        
        transcript_item = response['Item']
        
        # Parse transcript data
        try:
            transcription = json.loads(transcript_item.get('transcript', '{}'))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid transcript data format"},
                status_code=400
            )
        
        # Check if conversation data exists
        if "conversation" not in transcription or not transcription["conversation"]:
            return JSONResponse(
                {"error": "No conversation data found in transcript"},
                status_code=400
            )
        
        # Generate AI summary
        main_logger.info(f"Generating AI summary for transcript {transcript_id}")
        summary_prompt = "Please provide a concise summary of the following medical conversation:\n\n"
        for entry in transcription["conversation"]:
            summary_prompt += f"{entry['speaker']}: {entry['text']}\n\n"
        
        try:
            summary_response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are an expert medical documentation assistant. When summarizing conversations, do not use speaker labels like 'Speaker 0' or 'Speaker 1'. Instead, refer to participants by their roles (e.g., doctor/clinician and patient) based on context, or simply summarize the key medical information without attributing statements to specific speakers."},
                    {"role": "user", "content": summary_prompt}
                ],
                max_tokens=1000
            )
            
            ai_summary = summary_response.choices[0].message.content
            main_logger.info(f"AI summary generated successfully for transcript {transcript_id}")
        except Exception as e:
            error_msg = f"Error generating AI summary: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse(
                {"error": error_msg},
                status_code=500
            )
        
        # Return the summary without updating the transcript
        return {
            "success": True,
            "transcript_id": transcript_id,
            "ai_summary": ai_summary
        }
        
    except Exception as e:
        error_logger.exception(f"Error generating AI summary: {str(e)}")
        return JSONResponse(
            {"error": f"Failed to generate AI summary: {str(e)}"},
            status_code=500
        )


@app.get("/transcription-history")
async def get_transcription_history():
    """
    Get history of all transcriptions stored in the database.
    """
    try:
        table = dynamodb.Table('transcripts')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Include report IDs and summary in the response
        for item in items:
            item['report_ids'] = item.get('report_ids', [])
            item['ai_summary'] = item.get('ai_summary', None)
        
        return {"transcriptions": items}
    except Exception as e:
        error_logger.error(f"Error retrieving transcription history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve transcription history: {str(e)}"},
            status_code=500
        )

@app.get("/transcription/{transcript_id}")
async def get_transcription_detail(transcript_id: str):
    """
    Get detailed information about a specific transcription
    """
    try:
        # Get transcript data
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})
        
        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )
            
        transcript_item = response['Item']
        
        # Parse JSON data
        try:
            transcript = json.loads(transcript_item.get('transcript', '{}'))
            audio_file = json.loads(transcript_item.get('audio_file', '{}'))
        except:
            transcript = {}
            audio_file = {}
        
        # Get report if available
        report_data = None
        reports_table = dynamodb.Table('reports')
        report_response = reports_table.scan(
            FilterExpression="transcript_id = :transcript_id",
            ExpressionAttributeValues={":transcript_id": transcript_id}
        )
        report_items = report_response.get('Items', [])
        
        if report_items:
            report_item = report_items[0]
            report_data = {
                "id": report_item.get('id'),
                "template_type": report_item.get('template_type'),
                "formatted_report": report_item.get('formatted_report'),
                "gpt_response": json.loads(report_item.get('gpt_response', '{}')),
                "created_at": report_item.get('created_at')
            }
        
        # Generate presigned URL for audio
        presigned_url = None
        if audio_file.get('s3_path'):
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': audio_file.get('s3_path')},
                ExpiresIn=3600
            )
        
        # Prepare the response
        response_data = {
            "id": transcript_item.get('id'),
            "created_at": transcript_item.get('created_at'),
            "updated_at": transcript_item.get('updated_at'),
            "status": transcript_item.get('status'),
            "audio_info": {
                "filename": audio_file.get('filename'),
                "s3_path": audio_file.get('s3_path'),
                "presigned_url": presigned_url
            },
            "transcript": transcript,
            "report": report_data
        }
        
        return response_data
    except Exception as e:
        error_logger.error(f"Error retrieving transcription detail: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve transcription: {str(e)}"},
            status_code=500
        )

@app.get("/test-dynamodb")
async def test_dynamodb():
    table = dynamodb.Table('transcripts')
    response = table.scan(Limit=10)
    return {"items": response.get('Items', [])}

async def format_clinical_report(gpt_response):
    """
    Format a clinical report from GPT structured response based on CLINICAL_REPORT_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured clinical report data
        
    Returns:
        Formatted string containing the human-readable clinical report
    """
    try:
        report = []
        
        # Add heading
        report.append("# CLINICAL REPORT\n")
        
        # Define sections with their corresponding titles
        sections = {
            "presenting_problems": "## Presenting Problems",
            "history_of_problems": "## History of Problems",
            "current_functioning": "## Current Functioning",
            "current_medications": "## Current Medications",
            "psychiatric_history": "## Psychiatric History",
            "medical_history": "## Medical History",
            "developmental_social_family_history": "## Developmental, Social, and Family History",
            "substance_use": "## Substance Use",
            "cultural_religious_spiritual_issues": "## Cultural, Religious, and Spiritual Issues",
            "risk_assessment": "## Risk Assessment",
            "mental_state_exam": "## Mental State Examination",
            "test_results": "## Test Results",
            "diagnosis": "## Diagnosis",
            "clinical_formulation": "## Clinical Formulation"
        }
        
        # Iterate over each section and add to the report if not "Not discussed"
        for key, title in sections.items():
            value = gpt_response.get(key)
            if value and value != "Not discussed":
                report.append(f"{title}")
                report.append(value)
                report.append("")  # Add a newline for spacing
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting clinical report: {str(e)}", exc_info=True)
        return f"Error formatting clinical report: {str(e)}"

async def format_soap_note(gpt_response):
    """
    Format a SOAP note from GPT structured response based on SOAP_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        report = []
        
        # Add heading
        report.append("# SOAP NOTE\n")
        
        # Subjective section
        report.append("## Subjective:")
        if "subjective" in gpt_response:
            subjective = gpt_response["subjective"]
            has_content = False
            
            # Process subjective items, preserving any date information
            for key, value in subjective.items():
                if value and value != "Not discussed":
                    report.append(f"- {value}")
                    has_content = True
            
            if not has_content:
                report.append("No subjective findings documented")
        else:
            report.append("No subjective findings documented")
        
        report.append("")  # Add spacing between sections
        
        # Past Medical History section
        report.append("## Past Medical History:")
        if "past_medical_history" in gpt_response:
            past_medical = gpt_response["past_medical_history"]
            has_content = False
            
            for key, value in past_medical.items():
                if value and value != "Not discussed":
                    report.append(f"- {value}")
                    has_content = True
            
            if not has_content:
                report.append("No past medical history documented")
        else:
            report.append("No past medical history documented")
        
        report.append("")  # Add spacing between sections
        
        # Objective section - SIMPLIFIED WITH DIRECT BULLET POINTS WITHOUT FIELD NAMES
        report.append("## Objective:")
        if "objective" in gpt_response:
            objective = gpt_response["objective"]
            has_content = False
            
            # Direct bullet points for vital signs - no field names
            if "vital_signs" in objective:
                for key, value in objective["vital_signs"].items():
                    if value and value != "Not discussed":
                        # Simply add the value without the field name
                        report.append(f"- {value}")
                        has_content = True
            
            # Direct bullet points for physical exam findings - no field names
            if "physical_exam" in objective:
                if isinstance(objective["physical_exam"], dict):
                    for key, value in objective["physical_exam"].items():
                        if value and value != "Not discussed":
                            report.append(f"- {value}")
                            has_content = True
                elif isinstance(objective["physical_exam"], str) and objective["physical_exam"] != "Not discussed":
                    report.append(f"- {objective['physical_exam']}")
                    has_content = True
            
            # Direct bullet points for investigations results - no field names
            if "investigations_results" in objective:
                if isinstance(objective["investigations_results"], dict):
                    for key, value in objective["investigations_results"].items():
                        if value and value != "Not discussed":
                            report.append(f"- {value}")
                            has_content = True
                elif isinstance(objective["investigations_results"], str) and objective["investigations_results"] != "Not discussed":
                    report.append(f"- {objective['investigations_results']}")
                    has_content = True
            
            if not has_content:
                report.append("No objective findings documented")
        else:
            report.append("No objective findings documented")
        
        report.append("")  # Add spacing between sections
        
        # Assessment section
        report.append("## Assessment:")
        if "assessment" in gpt_response and gpt_response["assessment"]:
            assessment_items = gpt_response["assessment"]
            
            if not assessment_items or all(not item.get("issue") or item.get("issue") == "Not discussed" for item in assessment_items):
                report.append("No assessment documented")
            else:
                # Format assessment items as numbered list with issue and diagnosis
                for i, item in enumerate(assessment_items, 1):
                    issue = item.get("issue")
                    diagnosis = item.get("diagnosis")
                    differential = item.get("differential_diagnosis")
                    
                    if issue and issue != "Not discussed":
                        report_line = f"{i}. {issue}"
                        
                        if diagnosis and diagnosis != "Not discussed":
                            report_line += f"\nDiagnosis: {diagnosis}"
                        else:
                            report_line += "\nDiagnosis: not made"
                            
                        if differential and differential != "Not discussed":
                            report_line += f"\nDifferential: {differential}"
                            
                        report.append(report_line)
        else:
            report.append("No assessment documented")
        
        report.append("")  # Add spacing between sections
        
        # Plan section
        report.append("## Plan:")
        if "plan" in gpt_response:
            plan = gpt_response["plan"]
            has_content = False
            
            # Check if plan is a list (old format) or dictionary (new format)
            if isinstance(plan, list):
                # If plan is a list, just add each item as a bullet point
                filtered_items = [item for item in plan if item and item != "Not discussed"]
                if filtered_items:
                    for item in filtered_items:
                        report.append(f"- {item}")
                        has_content = True
            else:
                # Handle plan as a dictionary (standard format)
                # Investigations planned
                if plan.get("investigations_planned"):
                    investigations = [inv for inv in plan["investigations_planned"] if inv != "Not discussed"]
                    if investigations:
                        for inv in investigations:
                            report.append(f"- {inv}")
                            has_content = True
                
                # Treatment plan
                if plan.get("treatment_plan"):
                    treatment_plans = [p for p in plan["treatment_plan"] if p != "Not discussed"]
                    if treatment_plans:
                        for plan_item in treatment_plans:
                            report.append(f"- {plan_item}")
                            has_content = True
                
                # Follow-up
                if plan.get("followup"):
                    followups = [f for f in plan["followup"] if f != "Not discussed"]
                    if followups:
                        for followup in followups:
                            report.append(f"- {followup}")
                            has_content = True
                
                # Referrals
                if plan.get("referrals"):
                    referrals_list = [r for r in plan["referrals"] if r != "Not discussed"]
                    if referrals_list:
                        for referral in referrals_list:
                            report.append(f"- {referral}")
                            has_content = True
                
                # Other actions
                if plan.get("other_actions"):
                    actions = [action for action in plan["other_actions"] if action != "Not discussed"]
                    if actions:
                        for action in actions:
                            report.append(f"- {action}")
                            has_content = True
            
            if not has_content:
                report.append("No plan documented")
        else:
            report.append("No plan documented")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting SOAP note: {str(e)}"


async def format_new_soap(gpt_response):
    """
    Format a new SOAP note from GPT structured response based on NEW_SOAP_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        note = []
        
        # Add heading
        note.append("# SOAP NOTE\n")
        
        # SUBJECTIVE
        note.append("## SUBJECTIVE")
        subjective = gpt_response.get("subjective", {})
        
        # Add each subjective component on a new line with bullet points
        
        # Reasons and Complaints
        if subjective.get("reasons_and_complaints"):
            complaints = ", ".join(subjective["reasons_and_complaints"])
            note.append(f"- Patient presents with {complaints}.")
        
        # Duration Details
        if subjective.get("duration_details"):
            note.append(f"- {subjective['duration_details']}")
        
        # Modifying Factors
        if subjective.get("modifying_factors"):
            note.append(f"- {subjective['modifying_factors']}")
        
        # Progression
        if subjective.get("progression"):
            note.append(f"- {subjective['progression']}")
        
        # Previous Episodes
        if subjective.get("previous_episodes"):
            note.append(f"- {subjective['previous_episodes']}")
        
        # Impact on Daily Activities
        if subjective.get("impact_on_daily_activities"):
            note.append(f"- {subjective['impact_on_daily_activities']}")
        
        # Associated Symptoms
        if subjective.get("associated_symptoms"):
            symptoms = ", ".join(subjective["associated_symptoms"])
            note.append(f"- Associated symptoms include {symptoms}.")
        
        note.append("")
        
        # PAST MEDICAL HISTORY
        note.append("## PAST MEDICAL HISTORY")
        pmh = gpt_response.get("past_medical_history", {})
        
        # Add all PMH items as bullet points
        if pmh.get("contributing_factors"):
            note.append(f"- {pmh['contributing_factors']}")
            
        if pmh.get("exposure_history"):
            note.append(f"- {pmh['exposure_history']}")
            
        if pmh.get("immunization_history"):
            note.append(f"- {pmh['immunization_history']}")
            
        if pmh.get("other_relevant_info"):
            note.append(f"- {pmh['other_relevant_info']}")
            
        note.append("")
        
        # SOCIAL HISTORY
        if gpt_response.get("social_history"):
            note.append("## SOCIAL HISTORY")
            note.append(gpt_response["social_history"])
            note.append("")
            
        # FAMILY HISTORY
        if gpt_response.get("family_history"):
            note.append("## FAMILY HISTORY")
            note.append(gpt_response["family_history"])
            note.append("")
            
        # OBJECTIVE
        note.append("## OBJECTIVE")
        objective = gpt_response.get("objective", {})
        
        # Vital Signs
        vital_signs = objective.get("vital_signs", {})
        if any(vital_signs.values()):
            note.append("### Vital Signs:")
            vital_sign_labels = {
                "bp": "Blood Pressure",
                "hr": "Heart Rate",
                "wt": "Weight",
                "t": "Temperature",
                "o2": "O2 Saturation",
                "ht": "Height"
            }
            for key, label in vital_sign_labels.items():
                if vital_signs.get(key):
                    note.append(f"- {label}: {vital_signs[key]}")
            note.append("")
            
        # Physical Exam
        if objective.get("physical_exam"):
            note.append("### Physical Examination:")
            note.append(objective["physical_exam"])
            note.append("")
            
        # Investigations
        if objective.get("investigations"):
            note.append("### Investigations:")
            note.append(objective["investigations"])
            note.append("")
            
        # ASSESSMENT & PLAN
        note.append("## ASSESSMENT & PLAN")
        assessment_plan = gpt_response.get("assessment_plan", [])
        
        for i, issue in enumerate(assessment_plan, 1):
            if issue.get("issue"):
                note.append(f"\n### {i}. {issue['issue']}")
                
                if issue.get("assessment"):
                    note.append(f"\nAssessment: {issue['assessment']}")
                    
                if issue.get("differential_diagnosis"):
                    note.append("\nDifferential Diagnosis:")
                    for dx in issue["differential_diagnosis"]:
                        note.append(f"- {dx}")
                        
                if issue.get("investigations"):
                    note.append("\nInvestigations Planned:")
                    for investigation in issue["investigations"]:
                        note.append(f"- {investigation}")
                        
                if issue.get("treatment"):
                    note.append("\nTreatment:")
                    for treatment in issue["treatment"]:
                        note.append(f"- {treatment}")
                        
                if issue.get("referrals"):
                    note.append("\nReferrals:")
                    for referral in issue["referrals"]:
                        note.append(f"- {referral}")
                        
                if issue.get("follow_up_plan"):
                    note.append("\nFollow-up Plan:")
                    note.append(f"- {issue['follow_up_plan']}")
                        
                note.append("")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting new SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting new SOAP note: {str(e)}"


async def format_progress_note(data):
    """
    Format a progress note from GPT structured response.
    
    Args:
        data: Dictionary containing the structured progress note data
        
    Returns:
        String containing the formatted progress note
    """
    try:
        # Format current date if not provided
        if not data.get("note_date"):
            data["note_date"] = datetime.now().strftime("%d %B %Y")
            
        # Build the note
        note = []
        
        # Clinic letterhead
        
        # Clinic contact information
        if data.get("clinic_info", {}).get("address_line1"):
            note.append(data["clinic_info"]["address_line1"])
        if data.get("clinic_info", {}).get("address_line2"):
            note.append(data["clinic_info"]["address_line2"])
        if data.get("clinic_info", {}).get("phone"):
            note.append(data["clinic_info"]["phone"])
        if data.get("clinic_info", {}).get("fax"):
            note.append(data["clinic_info"]["fax"])
        note.append("")
        
        # Practitioner information
        practitioner_name = data.get("practitioner", {}).get("name", "")
        practitioner_title = data.get("practitioner", {}).get("title", "")
        if practitioner_name or practitioner_title:
            practitioner_info = f"Practitioner: {practitioner_name}"
            if practitioner_title:
                practitioner_info += f", {practitioner_title}"
            note.append(practitioner_info)
            note.append("")
        
        # Patient information
        if data.get("patient", {}).get("surname"):
            note.append(f"Surname: {data['patient']['surname']}")
        if data.get("patient", {}).get("firstname"):
            note.append(f"First Name: {data['patient']['firstname']}")
        if data.get("patient", {}).get("dob"):
            note.append(f"Date of Birth: {data['patient']['dob']}")
        note.append("")
        
        # Progress Note header
        note.append("# PROGRESS NOTE\n")
        
        # Date of note
        note.append(data.get("note_date", ""))
        note.append("")
        
        # Introduction
        if data.get("introduction"):
            note.append(data["introduction"])
            note.append("")
        
        # Patient history
        if data.get("history"):
            note.append(data["history"])
            note.append("")
        
        # Presentation
        if data.get("presentation"):
            note.append(data["presentation"])
            note.append("")
        
        # Mood and mental state
        if data.get("mood_mental_state"):
            note.append(data["mood_mental_state"])
            note.append("")
        
        # Social and functional status
        if data.get("social_functional"):
            note.append(data["social_functional"])
            note.append("")
        
        # Physical health
        if data.get("physical_health"):
            note.append(data["physical_health"])
            note.append("")
        
        # Plan and recommendations
        if data.get("plan"):
            note.append("## Plan and Recommendations:")
            for i, item in enumerate(data["plan"], 1):
                note.append(f"{i}. {item}")
            note.append("")
        
        # Closing
        if data.get("closing"):
            note.append(data["closing"])
            note.append("")
        
        # Practitioner signature
        if practitioner_name or practitioner_title:
            full_title = f"{practitioner_name}"
            if practitioner_title:
                full_title += f", {practitioner_title}"
            note.append(full_title)
            note.append("Consultant Psychiatrist")
        
        return "\n".join(note)
        
    except Exception as e:
        error_logger.error(f"Error formatting progress note: {str(e)}", exc_info=True)
        return f"Error formatting progress note: {str(e)}"

async def format_mental_health_note(gpt_response):
    """
    Format a mental health note from GPT structured response based on MENTAL_HEALTH_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured mental health note data
        
    Returns:
        Formatted string containing the human-readable mental health note
    """
    try:
        report = []
        
        # Add heading
        report.append("# MENTAL HEALTH APPOINTMENT NOTE\n")
        
        # Define sections with their corresponding titles
        sections = {
            "patient_details": "## Patient Details",
            "reason_for_visit": "## Reason for Visit",
            "presenting_issue": "## Presenting Issue",
            "past_psychiatric_history": "## Past Psychiatric History",
            "current_medications": "## Current Medications",
            "mental_status": "## Mental Status",
            "assessment": "## Assessment",
            "treatment_plan": "## Treatment Plan",
            "safety_assessment": "## Safety Assessment",
            "support": "## Support",
            "next_steps": "## Next Steps",
            "provider": "## Provider"
        }
        
        # Define patient fields if needed
        patient_fields = [
            ("name", "Name"),
            ("age", "Age"),
            ("gender", "Gender"),
            ("dob", "Date of Birth")
        ]
        
        # Iterate over each section and add to the report if not "Not discussed"
        for key, title in sections.items():
            value = gpt_response.get(key)
            if isinstance(value, dict):
                # For nested dictionaries, check if all values are "Not discussed"
                if all(v == "Not discussed" for v in value.values()):
                    continue
                # Otherwise, format the nested dictionary
                report.append(f"{title}")
                for sub_key, sub_value in value.items():
                    if sub_value != "Not discussed":
                        report.append(f"{sub_key.replace('_', ' ').title()}: {sub_value}")
            elif value and value != "Not discussed":
                report.append(f"{title}")
                report.append(value)
            
            report.append("")  # Add a newline for spacing
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting mental health note: {str(e)}", exc_info=True)
        return f"Error formatting mental health note: {str(e)}"

async def format_cardiology_letter(gpt_response):
    """
    Format a cardiology letter from GPT structured response based on CARDIOLOGY_LETTER_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured cardiology letter data
        
    Returns:
        Formatted string containing the human-readable cardiology letter
    """
    try:
        report = []
        
        # Add heading
        report.append("# CARDIOLOGY LETTER\n")
        
        # Check if this is an echocardiogram report or consultation letter
        is_echo_report = gpt_response.get("is_echocardiogram_report", False)
        
        # ---- HEADER SECTION ----
        if "doctor_details" in gpt_response and gpt_response["doctor_details"]:
            doctor = gpt_response["doctor_details"]
            header_lines = []
            
            if doctor.get("name") and doctor.get("name") != "Not discussed":
                header_lines.append(f"Dr {doctor['name']}")
            
            if doctor.get("credentials") and doctor.get("credentials") != "Not discussed":
                header_lines.append(doctor['credentials'])
            
            if doctor.get("provider_number") and doctor.get("provider_number") != "Not discussed":
                header_lines.append(f"Provider: {doctor['provider_number']}")
            
            if doctor.get("healthlink") and doctor.get("healthlink") != "Not discussed":
                header_lines.append(f"Healthlink: {doctor['healthlink']}")
            
            if doctor.get("practice_address") and doctor.get("practice_address") != "Not discussed":
                header_lines.append(doctor['practice_address'])
            
            if doctor.get("phone") and doctor.get("phone") != "Not discussed":
                header_lines.append(f"Phone: {doctor['phone']}")
            
            if doctor.get("fax") and doctor.get("fax") != "Not discussed":
                header_lines.append(f"Fax: {doctor['fax']}")
            
            for line in header_lines:
                report.append(f"{line:>120}")
            
            report.append("")
        
        # ---- REFERRAL DETAILS SECTION ----
        if "referral_details" in gpt_response and gpt_response["referral_details"]:
            ref = gpt_response["referral_details"]
            referral_lines = []
            
            if ref.get("referring_doctor") and ref.get("referring_doctor") != "Not discussed":
                referral_lines.append(f"Referring Doctor: Dr {ref['referring_doctor']}")
            
            if ref.get("practice_name") and ref.get("practice_name") != "Not discussed":
                referral_lines.append(f"Practice Name: {ref['practice_name']}")
            
            if ref.get("practice_address") and ref.get("practice_address") != "Not discussed":
                referral_lines.append(f"Practice Address: {ref['practice_address']}")
            
            if ref.get("practice_phone") and ref.get("practice_phone") != "Not discussed":
                referral_lines.append(f"Phone: {ref['practice_phone']}")
            
            if ref.get("practice_fax") and ref.get("practice_fax") != "Not discussed":
                referral_lines.append(f"Fax: {ref['practice_fax']}")
            
            for line in referral_lines:
                report.append(line)
            
            report.append("")
        
        # ---- PATIENT DETAILS SECTION ----
        if "patient_details" in gpt_response and gpt_response["patient_details"]:
            patient = gpt_response["patient_details"]
            patient_lines = []
            
            if patient.get("name") and patient.get("name") != "Not discussed":
                patient_lines.append(f"Patient Name: {patient['name']}")
            
            if patient.get("dob") and patient.get("dob") != "Not discussed":
                patient_lines.append(f"Date of Birth: {patient['dob']}")
            
            if patient.get("address") and patient.get("address") != "Not discussed":
                patient_lines.append(f"Address: {patient['address']}")
            
            if patient.get("phone") and patient.get("phone") != "Not discussed":
                patient_lines.append(f"Phone: {patient['phone']}")
            
            if patient.get("mobile") and patient.get("mobile") != "Not discussed":
                patient_lines.append(f"Mobile: {patient['mobile']}")
            
            for line in patient_lines:
                report.append(line)
            
            report.append("")
        
        # ---- MEDICAL HISTORY SECTION ----
        if "medical_history" in gpt_response and gpt_response["medical_history"]:
            medical_history = [condition for condition in gpt_response["medical_history"] if condition != "Not discussed"]
            if medical_history:
                report.append("## Medical History")
                for condition in medical_history:
                    report.append(f"- {condition}")
                report.append("")
        
        report.append("")
        # ---- MEDICATIONS SECTION ----
        if "medications" in gpt_response and gpt_response["medications"] != "Not discussed":
            report.append("## Medications")
            report.append(gpt_response["medications"])
            report.append("")
        report.append("")
        
        # ---- CONSULTATION NOTES SECTION ----
        if "consultation_notes" in gpt_response and gpt_response["consultation_notes"] != "Not discussed":
            report.append("##History")
            report.append(gpt_response["consultation_notes"])
            report.append("")
        report.append("")
        
        # ---- EXAMINATION FINDINGS SECTION ----
        if "examination_findings" in gpt_response and gpt_response["examination_findings"] != "Not discussed":
            report.append("## Examination Findings")
            report.append(gpt_response["examination_findings"])
            report.append("")
        report.append("")
        
        # ---- CURRENT PROBLEMS SECTION ----
        if "current_problems" in gpt_response and gpt_response["current_problems"] != "Not discussed":
            report.append("## Summary of Current Problems")
            report.append(gpt_response["current_problems"])
            report.append("")
        report.append("")
        
        # ---- PLAN & RECOMMENDATIONS SECTION ----
        if "plan_recommendations" in gpt_response and gpt_response["plan_recommendations"] != "Not discussed":
            report.append("## Plan and Recommendations")
            report.append(gpt_response["plan_recommendations"])
            report.append("")
        report.append("")
        
        # ---- CLOSING SECTION ----
        if "closing" in gpt_response and gpt_response["closing"] != "Not discussed":
            report.append(gpt_response["closing"])
            report.append("")
        
        # ---- SIGNATURE SECTION ----
        report.append("Yours sincerely,")
        report.append("")
        if "doctor_details" in gpt_response and gpt_response["doctor_details"]:
            doctor = gpt_response["doctor_details"]
            if doctor.get("name") and doctor.get("name") != "Not discussed":
                report.append(f"Dr {doctor['name']}")
            if doctor.get("credentials") and doctor.get("credentials") != "Not discussed":
                report.append(doctor['credentials'])
            report.append("Cardiologist")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting cardiology letter: {str(e)}", exc_info=True)
        return f"Error formatting cardiology letter: {str(e)}"

async def format_detailed_soap_note(gpt_response):
    """
    Format a detailed SOAP note from GPT structured response based on DETAILED_SOAP_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured SOAP note data
        
    Returns:
        Formatted string containing the human-readable SOAP note
    """
    try:
        note = []
        
        # Add title
        note.append("# Detailed SOAP Note\n")
        
        # Subjective section
        note.append("## Subjective:")
        subjective = gpt_response.get("subjective", {})
        subjective_documented = False
        
        subjective_fields = [
            ("current_issues", "- "),
            ("past_medical_history", "- "),
            ("medications", "- "),
            ("social_history", "- "),
            ("allergies", "- ")
        ]
        
        for field, prefix in subjective_fields:
            if subjective.get(field) and subjective[field] != "Not documented":
                note.append(f"{prefix}{subjective[field]}")
                subjective_documented = True
                
        if not subjective_documented:
            note.append("- No subjective information documented during this encounter.")
            
        note.append("")
        
        # Objective section
        note.append("## Objective:")
        objective = gpt_response.get("objective", {})
        objective_documented = False
        
        objective_fields = [
            ("vital_signs", "- "),
            ("physical_examination", "- "),
            ("laboratory_results", "- "),
            ("imaging_results", "- "),
            ("other_diagnostics", "- ")
        ]
        
        for field, prefix in objective_fields:
            if objective.get(field) and objective[field] != "Not documented":
                note.append(f"{prefix}{objective[field]}")
                objective_documented = True
                
        if not objective_documented:
            note.append("- No objective findings documented during this encounter.")
            
        note.append("")
        
        # Assessment section
        note.append("## Assessment:")
        assessment = gpt_response.get("assessment", {})
        assessment_documented = False
        
        assessment_fields = [
            ("diagnosis", "- "),
            ("clinical_impression", "- ")
        ]
        
        for field, prefix in assessment_fields:
            if assessment.get(field) and assessment[field] != "Not documented":
                note.append(f"{prefix}{assessment[field]}")
                assessment_documented = True
                
        if not assessment_documented:
            note.append("- No assessment documented during this encounter.")
            
        note.append("")
        
        # Plan section
        note.append("## Plan:")
        plan = gpt_response.get("plan", {})
        plan_documented = False
        
        plan_fields = [
            ("treatment", "- "),
            ("patient_education", "- "),
            ("referrals", "- "),
            ("additional_instructions", "- ")
        ]
        
        for field, prefix in plan_fields:
            if plan.get(field) and plan[field] != "Not documented":
                note.append(f"{prefix}{plan[field]}")
                plan_documented = True
                
        if not plan_documented:
            note.append("- No plan documented during this encounter.")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting detailed SOAP note: {str(e)}", exc_info=True)
        return f"Error formatting detailed SOAP note: {str(e)}"
    
async def format_followup_note(gpt_response):
    """
    Format a follow-up note from GPT structured response based on FOLLOWUP_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured follow-up note data
        
    Returns:
        Formatted string containing the human-readable follow-up note
    """
    try:
        report = []
        
        # Add title
        report.append("# FOLLOW UP NOTE\n")
        # Date
        if "date" in gpt_response and gpt_response["date"] and gpt_response["date"] != "Not documented":
            report.append(f"Date: {gpt_response['date']}")
        else:
            # Use current date if not provided or is "Not documented"
            current_date = datetime.now().strftime("%B %d, %Y")
            report.append(f"Date: {current_date}")
        report.append("")
        
        # History of Presenting Complaints
        report.append("## History of Presenting Complaints:")
        if "presenting_complaints" in gpt_response and gpt_response["presenting_complaints"]:
            if isinstance(gpt_response["presenting_complaints"], list) and len(gpt_response["presenting_complaints"]) > 0:
                for complaint in gpt_response["presenting_complaints"]:
                    if complaint != "Not documented" and complaint:
                        report.append(f"- {complaint}")
            elif isinstance(gpt_response["presenting_complaints"], str) and gpt_response["presenting_complaints"] != "Not documented":
                report.append(f"- {gpt_response['presenting_complaints']}")
            else:
                report.append("- Not documented")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Mental Status Examination
        report.append("## Mental Status Examination:")
        if "mental_status" in gpt_response and gpt_response["mental_status"]:
            mental_status = gpt_response["mental_status"]
            
            # Iterate through each mental status component
            for key, label in [
                ("appearance", "Appearance"),
                ("behavior", "Behavior"),
                ("speech", "Speech"),
                ("mood", "Mood"),
                ("affect", "Affect"),
                ("thoughts", "Thoughts"),
                ("perceptions", "Perceptions"),
                ("cognition", "Cognition"),
                ("insight", "Insight"),
                ("judgment", "Judgment")
            ]:
                value = mental_status.get(key, "Not documented")
                if value and value != "Not documented":
                    report.append(f"- {label}: {value}")
                else:
                    report.append(f"- {label}: Not documented")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Risk Assessment
        report.append("## Risk Assessment:")
        risk = gpt_response.get("risk_assessment", "Not documented")
        if risk and risk != "Not documented":
            report.append(f"- {risk}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Diagnosis
        report.append("## Diagnosis:")
        if "diagnosis" in gpt_response and gpt_response["diagnosis"]:
            if isinstance(gpt_response["diagnosis"], list):
                for diagnosis in gpt_response["diagnosis"]:
                    if diagnosis and diagnosis != "Not documented" and diagnosis != "None":
                        report.append(f"- {diagnosis}")
                if not any(d and d != "Not documented" and d != "None" for d in gpt_response["diagnosis"]):
                    report.append("- None")
            elif isinstance(gpt_response["diagnosis"], str) and gpt_response["diagnosis"] != "Not documented":
                report.append(f"- {gpt_response['diagnosis']}")
            else:
                report.append("- None")
        else:
            report.append("- None")
        report.append("")
        
        # Treatment Plan
        report.append("## Treatment Plan:")
        treatment_plan = gpt_response.get("treatment_plan", "Not documented")
        if treatment_plan and treatment_plan != "Not documented":
            # If treatment plan is a string, add it as a single bullet point
            if isinstance(treatment_plan, str):
                report.append(f"- {treatment_plan}")
            # If it's a list, add each item as a bullet point
            elif isinstance(treatment_plan, list):
                for plan_item in treatment_plan:
                    if plan_item and plan_item != "Not documented":
                        report.append(f"- {plan_item}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Safety Plan
        report.append("## Safety Plan:")
        safety_plan = gpt_response.get("safety_plan", "Not documented")
        if safety_plan and safety_plan != "Not documented":
            report.append(f"- {safety_plan}")
        else:
            report.append("- Not documented")
        report.append("")
        
        # Additional Notes
        report.append("## Additional Notes:")
        additional_notes = gpt_response.get("additional_notes", "None")
        if additional_notes and additional_notes != "Not documented" and additional_notes != "None":
            report.append(f"- {additional_notes}")
        else:
            report.append("- None")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting follow-up note: {str(e)}", exc_info=True)
        return f"Error formatting follow-up note: {str(e)}"
    
async def format_case_formulation(gpt_response):
    """
    Format a case formulation report following the 4Ps schema from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured case formulation data
        
    Returns:
        Formatted string containing the human-readable case formulation
    """
    try:
        # Handle the case where GPT returns an unexpected structure
        if "client_goals" not in gpt_response:
            # If we got an unexpected response structure, try to map it to our expected structure
            main_logger.warning("Received unexpected structure for case formulation")
            
            # Create a new response with default values
            mapped_response = {
                "client_goals": "Not documented",
                "presenting_problems": "Not documented",
                "predisposing_factors": "Not documented",
                "precipitating_factors": "Not documented",
                "perpetuating_factors": "Not documented",
                "protective_factors": "Not documented",
                "problem_list": "Not documented",
                "treatment_goals": "Not documented",
                "case_formulation": "Not documented",
                "treatment_mode": "Not documented"
            }
            
            # Check if we received a Patient Presentation structure
            if "Patient Presentation" in gpt_response:
                patient_data = gpt_response["Patient Presentation"]
                assessment = gpt_response.get("Assessment and Plan", {})
                
                # Try to map fields from the unexpected structure to our expected structure
                if "Chief Complaint" in patient_data:
                    mapped_response["presenting_problems"] = patient_data["Chief Complaint"]
                
                # Map symptoms to problem list
                if "History of Present Illness" in patient_data and "Symptoms" in patient_data["History of Present Illness"]:
                    symptoms = patient_data["History of Present Illness"]["Symptoms"]
                    problems = []
                    for category, description in symptoms.items():
                        if isinstance(description, str):
                            problems.append(f"{category}: {description}")
                        elif isinstance(description, dict):
                            for symptom, detail in description.items():
                                problems.append(f"{symptom}: {detail}")
                    if problems:
                        mapped_response["problem_list"] = problems
                
                # Map social history to protective factors
                if "Social History" in patient_data:
                    social = []
                    for key, value in patient_data["Social History"].items():
                        social.append(f"{key}: {value}")
                    if social:
                        mapped_response["protective_factors"] = social
                
                # Map diagnosis and treatment plan
                if "Diagnosis" in assessment:
                    mapped_response["case_formulation"] = f"The patient presents with {assessment['Diagnosis']}."
                
                if "Treatment Plan" in assessment:
                    treatment = []
                    for key, value in assessment["Treatment Plan"].items():
                        treatment.append(f"{key}: {value}")
                    if treatment:
                        mapped_response["treatment_mode"] = treatment
            
            # Use the mapped response
            gpt_response = mapped_response
        
        report = []
        
        # Add title
        report.append("# CASE FORMULATION 4PS\n")
        
        # CLIENT GOALS
        report.append("## CLIENT GOALS:")
        client_goals = gpt_response.get("client_goals", "Not documented")
        if client_goals and client_goals != "Not documented":
            report.append(client_goals)
        else:
            report.append("Client goals were not documented during the assessment.")
        report.append("")
        
        # PRESENTING PROBLEM/S
        report.append("## PRESENTING PROBLEM/S:")
        presenting_problems = gpt_response.get("presenting_problems", "Not documented")
        if presenting_problems and presenting_problems != "Not documented":
            if isinstance(presenting_problems, list):
                for problem in presenting_problems:
                    if problem and problem != "Not documented":
                        report.append(f"- {problem}")
            else:
                report.append(presenting_problems)
        else:
            report.append("No presenting problems were documented during the assessment.")
        report.append("")
        
        # PREDISPOSING FACTORS
        report.append("## PREDISPOSING FACTORS:")
        predisposing_factors = gpt_response.get("predisposing_factors", "Not documented")
        if predisposing_factors and predisposing_factors != "Not documented":
            if isinstance(predisposing_factors, list):
                for factor in predisposing_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(predisposing_factors)
        else:
            report.append("No predisposing factors were documented during the assessment.")
        report.append("")
        
        # PRECIPITATING FACTORS
        report.append("## PRECIPITATING FACTORS:")
        precipitating_factors = gpt_response.get("precipitating_factors", "Not documented")
        if precipitating_factors and precipitating_factors != "Not documented":
            if isinstance(precipitating_factors, list):
                for factor in precipitating_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(precipitating_factors)
        else:
            report.append("No precipitating factors were documented during the assessment.")
        report.append("")
        
        # PERPETUATING FACTORS
        report.append("## PERPETUATING FACTORS:")
        perpetuating_factors = gpt_response.get("perpetuating_factors", "Not documented")
        if perpetuating_factors and perpetuating_factors != "Not documented":
            if isinstance(perpetuating_factors, list):
                for factor in perpetuating_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(perpetuating_factors)
        else:
            report.append("No perpetuating factors were documented during the assessment.")
        report.append("")
        
        # PROTECTIVE FACTORS
        report.append("## PROTECTIVE FACTORS:")
        protective_factors = gpt_response.get("protective_factors", "Not documented")
        if protective_factors and protective_factors != "Not documented":
            if isinstance(protective_factors, list):
                for factor in protective_factors:
                    if factor and factor != "Not documented":
                        report.append(f"- {factor}")
            else:
                report.append(protective_factors)
        else:
            report.append("No protective factors were documented during the assessment.")
        report.append("")
        
        # PROBLEM LIST
        report.append("## PROBLEM LIST:")
        problem_list = gpt_response.get("problem_list", "Not documented")
        if problem_list and problem_list != "Not documented":
            if isinstance(problem_list, list):
                for i, problem in enumerate(problem_list, 1):
                    if problem and problem != "Not documented":
                        report.append(f"{i}. {problem}")
            else:
                report.append(problem_list)
        else:
            report.append("No problem list was documented during the assessment.")
        report.append("")
        
        # TREATMENT GOALS
        report.append("## TREATMENT GOALS:")
        treatment_goals = gpt_response.get("treatment_goals", "Not documented")
        if treatment_goals and treatment_goals != "Not documented":
            if isinstance(treatment_goals, list):
                for i, goal in enumerate(treatment_goals, 1):
                    if goal and goal != "Not documented":
                        report.append(f"{i}. {goal}")
            else:
                report.append(treatment_goals)
        else:
            report.append("No treatment goals were documented during the assessment.")
        report.append("")
        
        # CASE FORMULATION
        report.append("## CASE FORMULATION:")
        case_formulation = gpt_response.get("case_formulation", "Not documented")
        if case_formulation and case_formulation != "Not documented":
            report.append(case_formulation)
        else:
            report.append("No comprehensive case formulation was documented during the assessment.")
        report.append("")
        
        # TREATMENT MODE/INTERVENTIONS
        report.append("## TREATMENT MODE/INTERVENTIONS:")
        treatment_mode = gpt_response.get("treatment_mode", "Not documented")
        if treatment_mode and treatment_mode != "Not documented":
            if isinstance(treatment_mode, list):
                for mode in treatment_mode:
                    if mode and mode != "Not documented":
                        report.append(f"- {mode}")
            else:
                report.append(treatment_mode)
        else:
            report.append("No treatment mode or interventions were documented during the assessment.")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting case formulation: {str(e)}", exc_info=True)
        return f"Error formatting case formulation: {str(e)}"

async def format_meeting_minutes(gpt_response):
    """
    Format meeting minutes from GPT structured response based on MEETING_MINUTES_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured meeting minutes data
        
    Returns:
        Formatted string containing the human-readable meeting minutes
    """
    try:
        minutes = []
        
        # Add heading
        minutes.append("# MEETING MINUTES\n")
        
        # Date, Time, Location
        if gpt_response.get("date"):
            minutes.append(f"Date: {gpt_response['date']}")
        else:
            # Use current date if not provided
            current_date = datetime.now().strftime("%B %d, %Y")
            minutes.append(f"Date: {current_date}")
        
        if gpt_response.get("time"):
            minutes.append(f"Time: {gpt_response['time']}")
        else:
            # Use current time if not provided
            current_time = datetime.now().strftime("%I:%M %p")
            minutes.append(f"Time: {current_time}")
        
        if gpt_response.get("location"):
            minutes.append(f"Location: {gpt_response['location']}")
        else:
            minutes.append("Location: Not documented")
        
        minutes.append("")
        
        # Attendees
        minutes.append("## Attendees:")
        attendees = gpt_response.get("attendees", [])
        if attendees:
            for attendee in attendees:
                minutes.append(f"- {attendee}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Agenda Items
        minutes.append("## Agenda Items:")
        agenda_items = gpt_response.get("agenda_items", [])
        if agenda_items:
            for item in agenda_items:
                minutes.append(f"- {item}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Discussion Points
        minutes.append("## Discussion Points:")
        discussion_points = gpt_response.get("discussion_points", [])
        if discussion_points:
            for point in discussion_points:
                minutes.append(f"- {point}")
        else:
            minutes.append("- Not documented")
        
        minutes.append("")
        
        # Decisions Made
        minutes.append("## Decisions Made:")
        decisions = gpt_response.get("decisions_made", [])
        if decisions:
            for decision in decisions:
                minutes.append(f"- {decision}")
        else:
            minutes.append("- No decisions documented")
        
        minutes.append("")
        
        # Action Items
        minutes.append("## Action Items:")
        actions = gpt_response.get("action_items", [])
        if actions:
            for action in actions:
                minutes.append(f"- {action}")
        else:
            minutes.append("- No action items documented")
        
        minutes.append("")
        
        # Next Meeting
        minutes.append("## Next Meeting:")
        next_meeting = gpt_response.get("next_meeting", {})
        
        if next_meeting.get("date"):
            minutes.append(f"- Date: {next_meeting['date']}")
        else:
            minutes.append("- Date: Not scheduled")
            
        if next_meeting.get("time"):
            minutes.append(f"- Time: {next_meeting['time']}")
        else:
            minutes.append("- Time: Not scheduled")
            
        if next_meeting.get("location"):
            minutes.append(f"- Location: {next_meeting['location']}")
        else:
            minutes.append("- Location: Not determined")
        
        return "\n".join(minutes)
    except Exception as e:
        error_logger.error(f"Error formatting meeting minutes: {str(e)}", exc_info=True)
        return f"Error formatting meeting minutes: {str(e)}"
    
@app.delete("/report/{report_id}")
async def delete_report(report_id: str):
    """
    Delete a report, its associated transcription, and audio file.
    
    Args:
        report_id: ID of the report to delete
    
    Returns:
        Success or error message with details of deleted items
    """
    try:
        operation_id = str(uuid.uuid4())[:8]
        main_logger.info(f"[OP-{operation_id}] Delete request for report ID: {report_id}")
        
        # Get the report from DynamoDB to find associated transcript_id
        reports_table = dynamodb.Table('reports')
        report_response = reports_table.get_item(Key={"id": report_id})
        
        if 'Item' not in report_response:
            main_logger.warning(f"[OP-{operation_id}] Report ID {report_id} not found")
            return JSONResponse(
                {"error": f"Report ID {report_id} not found"},
                status_code=404
            )
        
        report_item = report_response['Item']
        transcript_id = report_item.get('transcript_id')
        template_type = report_item.get('template_type', 'unknown')
        main_logger.info(f"[OP-{operation_id}] Found report with template type: {template_type}, linked to transcript ID: {transcript_id}")
        
        # Get audio file info from transcript
        audio_info = None
        s3_path = None
        if transcript_id:
            transcripts_table = dynamodb.Table('transcripts')
            transcript_response = transcripts_table.get_item(Key={"id": transcript_id})
            
            if 'Item' in transcript_response:
                transcript_item = transcript_response['Item']
                try:
                    audio_info = json.loads(transcript_item.get('audio_file', '{}'))
                    s3_path = audio_info.get('s3_path')
                    main_logger.info(f"[OP-{operation_id}] Found audio file: {s3_path}")
                except json.JSONDecodeError:
                    main_logger.warning(f"[OP-{operation_id}] Could not parse audio info from transcript")
        
        # Delete the audio file from S3 if it exists
        if s3_path:
            try:
                main_logger.info(f"[OP-{operation_id}] Deleting audio file from S3: {s3_path}")
                s3_client.delete_object(
                    Bucket=S3_BUCKET,
                    Key=s3_path
                )
                main_logger.info(f"[OP-{operation_id}] Successfully deleted audio file from S3: {s3_path}")
            except Exception as e:
                main_logger.error(f"[OP-{operation_id}] Error deleting audio file from S3: {str(e)}")
        
        # Delete the report
        main_logger.info(f"[OP-{operation_id}] Deleting report from DynamoDB: {report_id}")
        reports_table.delete_item(Key={"id": report_id})
        
        # Delete the associated transcript if it exists
        if transcript_id:
            main_logger.info(f"[OP-{operation_id}] Deleting transcript from DynamoDB: {transcript_id}")
            transcripts_table = dynamodb.Table('transcripts')
            transcripts_table.delete_item(Key={"id": transcript_id})
            
        # Prepare response with details of what was deleted
        deletion_details = {
            "report_id": report_id,
            "transcript_id": transcript_id,
            "audio_file": s3_path
        }
        
        main_logger.info(f"[OP-{operation_id}] Deletion completed successfully: {deletion_details}")
        return {
            "success": True,
            "message": f"Report, transcript, and audio file deleted successfully",
            "deletion_details": deletion_details
        }
        
    except Exception as e:
        operation_id = locals().get('operation_id', str(uuid.uuid4())[:8])
        error_logger.error(f"[OP-{operation_id}] Error deleting report: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to delete report: {str(e)}"},
            status_code=500
        )

@app.get("/reports")
async def get_all_reports(limit: int = 100):
    """
    Get all reports stored in the database.
    
    Args:
        limit: Maximum number of reports to return (default: 100)
    
    Returns:
        List of all reports
    """
    try:
        # Query the reports table
        reports_table = dynamodb.Table('reports')
        response = reports_table.scan(Limit=limit)
        
        items = response.get('Items', [])
        
        # Format the response
        formatted_reports = []
        for item in items:
            # Parse the report data
            try:
                gpt_response = json.loads(item.get('gpt_response', '{}'))
            except:
                gpt_response = {}
                
            formatted_reports.append({
                "id": item.get('id'),
                "transcript_id": item.get('transcript_id'),
                "template_type": item.get('template_type'),
                "formatted_report": item.get('formatted_report'),
                "created_at": item.get('created_at'),
                "updated_at": item.get('updated_at'),
                "status": item.get('status')
            })
        
        return {"reports": formatted_reports}
        
    except Exception as e:
        error_logger.error(f"Error retrieving reports: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve reports: {str(e)}"},
            status_code=500
        )

@app.get("/summary-history")
async def get_summary_history():
    """
    Get history of all summaries stored in the database.
    """
    try:
        table = dynamodb.Table('summaries')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Return all items without filtering or limiting fields
        return {"summaries": items}
    except Exception as e:
        error_logger.error(f"Error retrieving summary history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve summary history: {str(e)}"},
            status_code=500
        )

@app.get("/report-history")
async def get_report_history():
    """
    Get history of all reports stored in the database.
    """
    try:
        table = dynamodb.Table('reports')
        response = table.scan()
        
        items = response.get('Items', [])
        
        # Return all items without filtering or limiting fields
        return {"reports": items}
    except Exception as e:
        error_logger.error(f"Error retrieving report history: {str(e)}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to retrieve report history: {str(e)}"},
            status_code=500
        )

@app.post("/transcribe-audio")
@log_execution_time
async def transcribe_audio(
    audio: UploadFile = File(...)
):
    """
    Transcribe an audio file and store the transcription in the transcripts table.
    
    Args:
        audio: WAV audio file to process
    """
    try:
        # Log incoming request details
        main_logger.info(f"Received audio transcription request - Filename: {audio.filename}, Content-Type: {audio.content_type}")

        # Validate content type
        valid_content_types = [
            # WAV formats
            "audio/wav", "audio/wave", "audio/x-wav",
            # MP3 formats
            "audio/mp3", "audio/mpeg", "audio/mpeg3", "audio/x-mpeg-3",
            # MP4/M4A formats
            "audio/mp4", "audio/x-m4a", "audio/m4a",
            # FLAC formats
            "audio/flac", "audio/x-flac",
            # AAC formats
            "audio/aac", "audio/x-aac",
            # OGG formats
            "audio/ogg", "audio/vorbis", "application/ogg",
            # WEBM formats
            "audio/webm",
            # Other common audio formats
            "audio/3gpp", "audio/amr"
        ]

        if audio.content_type not in valid_content_types:
            # Allow files with missing content type but with audio file extensions
            valid_extensions = [".wav", ".mp3", ".mp4", ".m4a", ".flac", ".aac", ".ogg", ".webm", ".amr", ".3gp"]
            file_extension = os.path.splitext(audio.filename.lower())[1]
            if file_extension in valid_extensions:
                main_logger.info(f"Audio content-type not recognized ({audio.content_type}), but filename has valid extension: {file_extension}")
            else:
                error_msg = f"Invalid audio format. Expected audio file, got {audio.content_type}"
                error_logger.error(error_msg)
                return JSONResponse({"error": error_msg}, status_code=400)

        # Read the audio file
        audio_data = await audio.read()
        if not audio_data:
            error_msg = "No audio data provided"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)

        main_logger.info(f"Audio file read successfully. Size: {len(audio_data)} bytes")

        # Transcribe audio
        transcription_result = await transcribe_audio_with_diarization(audio_data)
        
        # Save transcription to DynamoDB
        transcript_id = await save_transcript_to_dynamodb(
            transcription_result,
            None,  # No audio info needed for this endpoint
            status="completed"
        )
        
        if not transcript_id:
            error_msg = "Failed to save transcription to DynamoDB"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=500)
        
        # Return the transcription result
        return JSONResponse({
            "transcript_id": transcript_id,
            "transcription": transcription_result
        })

    except Exception as e:
        error_msg = f"Unexpected error in transcribe_audio: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

@app.get("/search")
async def search_data(
    summary_id: str = None,
    transcript_id: str = None,
    report_id: str = None
):
    """
    Search for data in the summaries, transcripts, or reports table based on the provided ID.
    
    Args:
        summary_id: ID of the summary to search for
        transcript_id: ID of the transcription to search for
        report_id: ID of the report to search for
    
    Returns:
        The data associated with the provided ID, or an error message if not found
    """
    try:
        if summary_id:
            table = dynamodb.Table('summaries')
            response = table.get_item(Key={"id": summary_id})
            if 'Item' in response:
                item = response['Item']
                decrypted_summary = decrypt_data(item['summary'])
                item['summary'] = decrypted_summary
                return {"summary": item}
            else:
                return JSONResponse({"error": f"Summary ID {summary_id} not found"}, status_code=404)
        
        if transcript_id:
            # Get the transcription data
            transcripts_table = dynamodb.Table('transcripts')
            transcript_response = transcripts_table.get_item(Key={"id": transcript_id})
            if 'Item' not in transcript_response:
                return JSONResponse({"error": f"Transcript ID {transcript_id} not found"}, status_code=404)
            
            transcription = transcript_response['Item']
            decrypted_transcription = decrypt_data(transcription['transcript'])
            transcription['transcript'] = decrypted_transcription

            # Get all reports associated with this transcription (PAGINATED)
            reports_table = dynamodb.Table('reports')
            from boto3.dynamodb.conditions import Attr  # Ensure this import is present at the top
            reports = await dynamodb_scan_all(
                reports_table,
                FilterExpression=Attr('transcript_id').eq(transcript_id)
            )
            
            # Add report IDs to the transcription data
            transcription['report_ids'] = [report['id'] for report in reports]
            decrypted_reports = [decrypt_data(report['gpt_response']) for report in reports]
            for report, decrypted in zip(reports, decrypted_reports):
                report['gpt_response'] = decrypted
            return {"transcription": transcription, "reports": reports}
        
        if report_id:
            table = dynamodb.Table('reports')
            response = table.get_item(Key={"id": report_id})
            if 'Item' in response:
                item = response['Item']
                if 'gpt_response' in item:
                    item['gpt_response'] = decrypt_data(item['gpt_response'])
                return {"report": item}
            else:
                return JSONResponse({"error": f"Report ID {report_id} not found"}, status_code=404)
        
        return JSONResponse({"error": "No valid ID provided"}, status_code=400)
    
    except Exception as e:
        error_logger.error(f"Error searching data: {str(e)}", exc_info=True)
        return JSONResponse({"error": f"Failed to search data: {str(e)}"}, status_code=500)

async def format_dietician_assessment(gpt_response):
    """
    Format a detailed dietician initial assessment from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured dietician assessment data
        
    Returns:
        Formatted string containing the human-readable dietician assessment
    """
    try:
        report = []
        # Title
        report.append("# DETAILED DIETICIAN INITIAL ASSESSMENT\n")
        
        # Weight History
        weight_history = gpt_response.get("weight_history")
        if weight_history and weight_history != "Not discussed":
            report.append("##Weight History")
            report.append(weight_history)
            report.append("")
        else:
            report.append("## Weight History")
            report.append("Weight History was not discussed in the initial assessment.")
            report.append("")

        # Consolidated Disordered Eating/Eating Disorder Behavior and Nutrition Intake
        disordered_eating_behavior = gpt_response.get("dietary_habits")
        if disordered_eating_behavior and disordered_eating_behavior != "Not discussed":
            report.append("## Disordered Eating/Eating Disorder Behavior and Nutrition Intake")
            report.append(disordered_eating_behavior)
            report.append("")
        else:
            report.append("## Disordered Eating/Eating Disorder Behavior and Nutrition Intake")
            report.append("Disordered Eating/Eating Disorder Behavior and Nutrition Intake were not discussed in the initial assessment.")
            report.append("")

        # Physical Activity Behavior
        physical_activity = gpt_response.get("physical_activity")
        if physical_activity and physical_activity != "Not discussed":
            report.append("## Physical Activity Behavior")
            report.append(physical_activity)
            report.append("")
        else:
            report.append("## Physical Activity Behavior")
            report.append("Physical Activity Behavior was not discussed in the initial assessment.")
            report.append("")

        # Medical & Psychiatric History
        medical_history = gpt_response.get("medical_history")
        if medical_history and medical_history != "Not discussed":
            report.append("## Medical & Psychiatric History")
            report.append(medical_history)
            report.append("")
        else:
            report.append("## Medical & Psychiatric History")
            report.append("Medical & Psychiatric History was not discussed in the initial assessment.")
            report.append("")

        # Medications/Supplements
        medications = gpt_response.get("medications")
        supplements = gpt_response.get("supplements")
        if (medications and medications != "Not discussed") or (supplements and supplements != "Not discussed"):
            report.append("## Medications/Supplements")
            if medications:
                report.append(f"Medications: {medications}")
            if supplements:
                report.append(f"Supplements: {supplements}")
            report.append("")
        else:
            report.append("## Medications/Supplements")
            report.append("Medications/Supplements were not discussed in the initial assessment.")
            report.append("")

        # Social History/Lifestyle
        social_history = gpt_response.get("goals")
        if social_history and social_history != "Not discussed":
            report.append("## Social History/Lifestyle")
            report.append(social_history)
            report.append("")
        else:
            report.append("## Social History/Lifestyle")
            report.append("Social History/Lifestyle was not discussed in the initial assessment.")
            report.append("")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting dietician assessment: {str(e)}", exc_info=True)
        return f"Error formatting dietician assessment: {str(e)}"

async def format_consult_note(gpt_response):
    """
    Format a consultation note from GPT structured response based on CONSULT_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured consultation note data
        
    Returns:
        Formatted string containing the human-readable consultation note
    """
    try:
        report = []
        
        # Add heading
        report.append("# CONSULTATION NOTE\n")
        
        # Add patient name and date if available
        if gpt_response.get("patient_name") and gpt_response["patient_name"] != "Not documented":
            report.append(f"Patient: {gpt_response['patient_name']}")
        
        if gpt_response.get("consultation_date") and gpt_response["consultation_date"] != "Not documented":
            report.append(f"Date: {gpt_response['consultation_date']}")
            
        report.append("")
        
        # Add consultation context with labels
        context = gpt_response.get("consultation_context", {})
        context_lines = []
        
        if context.get("consultation_type") and context["consultation_type"] != "Not documented":
            context_lines.append(f"Consultation Type: {context['consultation_type']}")
            
        if context.get("patient_status") and context["patient_status"] != "Not documented":
            context_lines.append(f"Patient Status: {context['patient_status']}")
            
        if context.get("reason_for_visit") and context["reason_for_visit"] != "Not documented":
            context_lines.append(f"Reason for Visit: {context['reason_for_visit']}")
            
        if context_lines:
            report.extend(context_lines)
        else:
            report.append("Consultation details not documented.")
            
        report.append("")
        
        # History section with hashtag for bold
        report.append("## History:")
        history = gpt_response.get("history", {})
        history_documented = False
        
        history_fields = [
            ("presenting_complaints", "- "),
            ("ideas_concerns_expectations", "- ICE: "),
            ("red_flag_symptoms", "- "),
            ("risk_factors", "- Relevant risk factors: "),
            ("past_medical_history", "- PMH: "),
            ("medications", "- DH: "),
            ("allergies", "- Allergies: "),
            ("family_history", "- FH: "),
            ("social_history", "- SH: ")
        ]
        
        for field, prefix in history_fields:
            if field in history and history[field] and history[field] != "Not discussed":
                report.append(f"{prefix}{history[field]}")
                history_documented = True
                
        if not history_documented:
            report.append("No history documented during this consultation.")
            
        report.append("")
        
        # Examination section with hashtag for bold
        report.append("## Examination:")
        examination = gpt_response.get("examination", {})
        examination_documented = False
        
        exam_fields = [
            ("vital_signs", "- "),
            ("physical_findings", "- "),
            ("investigations", "- ")
        ]
        
        for field, prefix in exam_fields:
            if field in examination and examination[field] and examination[field] != "Not documented":
                report.append(f"{prefix}{examination[field]}")
                examination_documented = True
                
        if not examination_documented:
            report.append("No examination documented during this consultation.")
            
        report.append("")
        
        # Impression section with hashtag for bold
        report.append("## Impression:")
        impression_items = gpt_response.get("impression", [])
        
        if impression_items and any(item.get("issue") and item["issue"] != "Not documented" for item in impression_items):
            for i, item in enumerate(impression_items, 1):
                if item.get("issue") and item["issue"] != "Not documented":
                    issue_text = item["issue"].strip()
                    
                    # Format the issue line
                    issue_line = f"{i}. {issue_text}"
                    
                    # Add diagnosis if available
                    if item.get("diagnosis") and item["diagnosis"] != "Not documented":
                        diagnosis_text = item["diagnosis"].strip()
                        issue_line += f". Likely diagnosis: {diagnosis_text}"
                        
                    report.append(issue_line)
                    
                    # Only include differential diagnosis if it exists and is not "Not documented"
                    if item.get("differential_diagnosis") and item["differential_diagnosis"] != "Not documented":
                        diff_diagnosis_text = item["differential_diagnosis"].strip()
                        report.append(f"- Differential diagnosis: {diff_diagnosis_text}")
        else:
            report.append("No clinical impression documented during this consultation.")
            
        report.append("")
        
        # Plan section with hashtag for bold
        report.append("## Plan:")
        plan = gpt_response.get("plan", {})
        plan_documented = False
        
        plan_fields = [
            ("investigations", "- Investigations planned: "),
            ("treatment", "- Treatment planned: "),
            ("referrals", "- Relevant referrals: "),
            ("follow_up", "- Follow up plan: "),
            ("safety_netting", "- Safety netting advice given: ")
        ]
        
        for field, prefix in plan_fields:
            if field in plan and plan[field] and plan[field] != "Not documented":
                report.append(f"{prefix}{plan[field]}")
                plan_documented = True
                
        if not plan_documented:
            report.append("No plan documented during this consultation.")
        
        return "\n".join(report)
    except Exception as e:
        error_logger.error(f"Error formatting consultation note: {str(e)}", exc_info=True)
        return f"Error formatting consultation note: {str(e)}"

async def format_referral_letter(gpt_response):
    """
    Format a referral letter from GPT structured response based on REFERRAL_LETTER_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured referral letter data
        
    Returns:
        Formatted string containing the human-readable referral letter
    """
    try:
        letter = []
        
        # Add heading
        letter.append("# REFERRAL LETTER\n")
        
        # Date
        if gpt_response.get("date"):
            letter.append(f"[{gpt_response['date']}]")
        else:
            # Use current date if not provided
            current_date = datetime.now().strftime("%d %B %Y")
            letter.append(f"[{current_date}]")
        
        letter.append("")
        
        # Recipient information
        letter.append("## To:")
        consultant = gpt_response.get("consultant", {})
        
        if consultant.get("name"):
            letter.append(f"{consultant['name']}")
        else:
            letter.append("[Consultant's Name]")
            
        if consultant.get("specialty") and consultant.get("hospital"):
            letter.append(f"{consultant['specialty']}, {consultant['hospital']}")
        elif consultant.get("hospital"):
            letter.append(f"{consultant['hospital']}")
        else:
            letter.append("[Specialist Clinic/Hospital Name]")
            
        # If address is available, split and format it
        if consultant.get("address"):
            address_parts = consultant["address"].split(",")
            for part in address_parts:
                letter.append(part.strip())
        else:
            letter.append("[Address Line]")
        
        letter.append("")
        
        # Salutation
        if consultant.get("name"):
            # Extract last name if possible
            name_parts = consultant["name"].split()
            if len(name_parts) > 1 and name_parts[0].lower().startswith("dr"):
                letter.append(f"Dear {name_parts[-1]},")
            else:
                letter.append(f"Dear {consultant['name']},")
        else:
            letter.append("Dear Dr. [Consultant's Last Name],")
        
        letter.append("")
        
        # Patient information
        patient = gpt_response.get("patient", {})
        if patient.get("name") and patient.get("dob"):
            letter.append(f"Re: Referral for {patient['name']}, Date of Birth: {patient['dob']}")
        elif patient.get("name"):
            letter.append(f"Re: Referral for {patient['name']}")
        else:
            letter.append("Re: Referral for [Patient's Name], [Date of Birth: DOB]")
        
        letter.append("")
        
        # Introduction
        if patient.get("name") and patient.get("condition"):
            letter.append(f"I am referring {patient['name']} to your clinic for further evaluation and management of {patient['condition']}.")
        elif patient.get("condition"):
            letter.append(f"I am referring this patient to your clinic for further evaluation and management of {patient['condition']}.")
        elif patient.get("name"):
            letter.append(f"I am referring {patient['name']} to your clinic for further evaluation and management.")
        else:
            letter.append("I am referring this patient to your clinic for further evaluation and management.")
        
        letter.append("")
        
        # Clinical Details
        letter.append("## Clinical Details:")
        clinical_details = gpt_response.get("clinical_details", {})
        clinical_details_documented = False
        
        if clinical_details.get("presenting_complaint"):
            letter.append(f"Presenting Complaint: {clinical_details['presenting_complaint']}")
            clinical_details_documented = True
            
        if clinical_details.get("duration"):
            letter.append(f"Duration: {clinical_details['duration']}")
            clinical_details_documented = True
            
        if clinical_details.get("relevant_findings"):
            letter.append(f"Relevant Findings: {clinical_details['relevant_findings']}")
            clinical_details_documented = True
            
        if clinical_details.get("past_medical_history"):
            letter.append(f"Past Medical History: {clinical_details['past_medical_history']}")
            clinical_details_documented = True
            
        if clinical_details.get("current_medications"):
            letter.append(f"Current Medications: {clinical_details['current_medications']}")
            clinical_details_documented = True
        
        if not clinical_details_documented:
            letter.append("No clinical details were documented during the consultation.")
        
        letter.append("")
        
        # Investigations
        letter.append("## Investigations:")
        investigations = gpt_response.get("investigations", {})
        investigations_documented = False
        
        if investigations.get("recent_tests"):
            letter.append(f"Recent Tests: {investigations['recent_tests']}")
            investigations_documented = True
            
        if investigations.get("results"):
            letter.append(f"Results: {investigations['results']}")
            investigations_documented = True
        
        if not investigations_documented:
            letter.append("No investigations were documented during the consultation.")
        
        letter.append("")
        
        # Reason for Referral
        letter.append("## Reason for Referral:")
        if gpt_response.get("reason_for_referral"):
            letter.append(f"{gpt_response['reason_for_referral']}")
        else:
            letter.append("No specific reason for referral was documented during the consultation.")
        
        letter.append("")
        
        # Patient Contact Information
        letter.append("## Patient's Contact Information:")
        patient_contact_documented = False
        
        if patient.get("phone"):
            letter.append(f"Phone Number: {patient['phone']}")
            patient_contact_documented = True
            
        if patient.get("email"):
            letter.append(f"Email Address: {patient['email']}")
            patient_contact_documented = True
        
        if not patient_contact_documented:
            letter.append("No patient contact information was documented during the consultation.")
        
        letter.append("")
        
        # Closing
        letter.append("Enclosed are relevant test results and reports for your review. Please do not hesitate to contact me if you require further information.")
        letter.append("")
        letter.append("Thank you for your attention to this referral. I look forward to your evaluation and recommendations.")
        letter.append("")
        letter.append("Yours sincerely,")
        letter.append("")
        
        # Referring Doctor
        doctor = gpt_response.get("referring_doctor", {})
        if doctor.get("name"):
            letter.append(f"{doctor['name']}")
        else:
            letter.append("[Your Full Name]")
            
        if doctor.get("title"):
            letter.append(f"{doctor['title']}")
        else:
            letter.append("[Your Title]")
            
        if doctor.get("contact"):
            letter.append(f"{doctor['contact']}")
        else:
            letter.append("[Your Contact Information]")
            
        if doctor.get("practice"):
            letter.append(f"{doctor['practice']}")
        else:
            letter.append("[Your Practice Name]")
        
        return "\n".join(letter)
    except Exception as e:
        error_logger.error(f"Error formatting referral letter: {str(e)}", exc_info=True)
        return f"Error formatting referral letter: {str(e)}"


# Add or update the format_psychology_session_notes function
async def format_psychology_session_notes(gpt_response):
    """
    Format psychology session notes from GPT structured response.
    
    Args:
        gpt_response: Dictionary containing the structured psychology session notes data
        
    Returns:
        Formatted string containing the human-readable psychology session notes
    """
    try:
        notes = []
        
        # Add heading
        notes.append("# PSYCHOLOGY SESSION NOTES\n")
        
        # OUT OF SESSION TASK REVIEW
        notes.append("## OUT OF SESSION TASK REVIEW:")
        session_tasks = gpt_response.get("session_tasks_review", {})
        if (session_tasks.get("practice_skills") or 
            session_tasks.get("task_effectiveness") or 
            session_tasks.get("challenges")):
            
            if session_tasks.get("practice_skills"):
                for item in session_tasks["practice_skills"]:
                    notes.append(f"- {item}")
                    
            if session_tasks.get("task_effectiveness"):
                for item in session_tasks["task_effectiveness"]:
                    notes.append(f"- {item}")
                    
            if session_tasks.get("challenges"):
                for item in session_tasks["challenges"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No out of session task review documented during session")
            
        notes.append("")
        
        # CURRENT PRESENTATION
        notes.append("## CURRENT PRESENTATION:")
        current = gpt_response.get("current_presentation", {})
        if current.get("current_symptoms") or current.get("changes"):
            if current.get("current_symptoms"):
                for item in current["current_symptoms"]:
                    notes.append(f"- {item}")
                    
            if current.get("changes"):
                for item in current["changes"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No current presentation documented during session")
            
        notes.append("")
        
        # SESSION CONTENT
        notes.append("## SESSION CONTENT:")
        session = gpt_response.get("session_content", {})
        if (session.get("issues_raised") or session.get("discussions") or 
            session.get("therapy_goals") or session.get("progress") or 
            session.get("main_topics")):
            
            if session.get("issues_raised"):
                for item in session["issues_raised"]:
                    notes.append(f"- {item}")
                    
            if session.get("discussions"):
                for item in session["discussions"]:
                    notes.append(f"- {item}")
                    
            if session.get("therapy_goals"):
                for item in session["therapy_goals"]:
                    notes.append(f"- {item}")
                    
            if session.get("progress"):
                for item in session["progress"]:
                    notes.append(f"- {item}")
                    
            if session.get("main_topics"):
                for item in session["main_topics"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No session content documented during session")
            
        notes.append("")
        
        # INTERVENTION
        notes.append("## INTERVENTION:")
        intervention = gpt_response.get("intervention", {})
        if intervention.get("techniques") or intervention.get("strategies"):
            if intervention.get("techniques"):
                for item in intervention["techniques"]:
                    notes.append(f"- {item}")
                    
            if intervention.get("strategies"):
                for item in intervention["strategies"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No intervention documented during session")
            
        notes.append("")
        
        # SETBACKS/BARRIERS/PROGRESS WITH TREATMENT
        notes.append("## SETBACKS/ BARRIERS/ PROGRESS WITH TREATMENT:")
        progress = gpt_response.get("treatment_progress", {})
        if progress.get("setbacks") or progress.get("satisfaction"):
            if progress.get("setbacks"):
                for item in progress["setbacks"]:
                    notes.append(f"- {item}")
                    
            if progress.get("satisfaction"):
                for item in progress["satisfaction"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No setbacks/barriers/progress with treatment documented during session")
            
        notes.append("")
        
        # RISK ASSESSMENT AND MANAGEMENT
        notes.append("## RISK ASSESSMENT AND MANAGEMENT:")
        risk = gpt_response.get("risk_assessment", {})
        has_risk_info = False
        
        for field in ["suicidal_ideation", "homicidal_ideation", "self_harm", "violence"]:
            if risk.get(field) and risk[field].strip():
                has_risk_info = True
                break
                
        if has_risk_info or (risk.get("management_plan") and risk["management_plan"]):
            if risk.get("suicidal_ideation") and risk["suicidal_ideation"].strip():
                notes.append(f"- Suicidal Ideation: {risk['suicidal_ideation']}")
                
            if risk.get("homicidal_ideation") and risk["homicidal_ideation"].strip():
                notes.append(f"- Homicidal Ideation: {risk['homicidal_ideation']}")
                
            if risk.get("self_harm") and risk["self_harm"].strip():
                notes.append(f"- Self-harm: {risk['self_harm']}")
                
            if risk.get("violence") and risk["violence"].strip():
                notes.append(f"- Violence & Aggression: {risk['violence']}")
                
            if risk.get("management_plan") and risk["management_plan"]:
                notes.append("")
                notes.append("### Management Plan:")
                for item in risk["management_plan"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No risk assessment and management documented during session")
            
        notes.append("")
        
        # MENTAL STATUS EXAMINATION
        notes.append("## MENTAL STATUS EXAMINATION:")
        mental = gpt_response.get("mental_status", {})
        has_mental_status = False
        
        for field in ["appearance", "behaviour", "speech", "mood", "affect", 
                     "thoughts", "perceptions", "cognition", "insight", "judgment"]:
            if mental.get(field) and mental[field].strip():
                has_mental_status = True
                break
                
        if has_mental_status:
            mental_status_fields = [
                ("appearance", "Appearance: "),
                ("behaviour", "Behaviour: "),
                ("speech", "Speech: "),
                ("mood", "Mood: "),
                ("affect", "Affect: "),
                ("thoughts", "Thoughts: "),
                ("perceptions", "Perceptions: "),
                ("cognition", "Cognition: "),
                ("insight", "Insight: "),
                ("judgment", "Judgment: ")
            ]
            
            for field, label in mental_status_fields:
                if mental.get(field) and mental[field].strip():
                    notes.append(f"{label}{mental[field]}")
        else:
            notes.append("No mental status examination documented during session")
            
        notes.append("")
        
        # OUT OF SESSION TASKS
        notes.append("## OUT OF SESSION TASKS:")
        tasks = gpt_response.get("out_of_session_tasks", [])
        if tasks:
            for item in tasks:
                notes.append(f"- {item}")
        else:
            notes.append("No out of session tasks documented during session")
                
        notes.append("")
        
        # PLAN FOR NEXT SESSION
        notes.append("## PLAN FOR NEXT SESSION:")
        next_session = gpt_response.get("next_session", {})
        if next_session.get("date") or (next_session.get("plan") and next_session["plan"]):
            if next_session.get("date"):
                notes.append(f"- Next Session: {next_session['date']}")
                
            if next_session.get("plan") and next_session["plan"]:
                for item in next_session["plan"]:
                    notes.append(f"- {item}")
        else:
            notes.append("No plan for next session documented during session")
        
        return "\n".join(notes)
    except Exception as e:
        error_logger.error(f"Error formatting psychology session notes: {str(e)}", exc_info=True)
        return f"Error formatting psychology session notes: {str(e)}"
    
async def format_discharge_summary(gpt_response):
    """
    Format a discharge summary from GPT structured response based on DISCHARGE_SUMMARY_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured discharge summary data
        
    Returns:
        Formatted string containing the human-readable discharge summary
    """
    try:
        summary = []
        
        # Title
        summary.append("# Discharge Summary:")
        summary.append("")
        
        # Client information
        client = gpt_response.get("client", {})
        
        # For client name, use placeholder if missing
        if client.get("name"):
            summary.append(f"Client Name: {client['name']}")
        else:
            summary.append("Client Name: [client name]")
            
        # For DOB, use placeholder if missing
        if client.get("dob"):
            summary.append(f"Date of Birth: {client['dob']}")
        else:
            summary.append("Date of Birth: [date of birth]")
            
        # For discharge date, use today's date if missing
        if client.get("discharge_date"):
            summary.append(f"Date of Discharge: {client['discharge_date']}")
        else:
            # Use current date
            current_date = datetime.now().strftime("%B %d, %Y")
            summary.append(f"Date of Discharge: {current_date}")
            
        summary.append("")
        
        # Referral Information
        summary.append("## Referral Information")
        referral = gpt_response.get("referral", {})
        referral_documented = False
        
        if referral.get("source"):
            summary.append(f"- Referral Source: {referral['source']}")
            referral_documented = True
            
        if referral.get("reason"):
            summary.append(f"- Reason for Referral: {referral['reason']}")
            referral_documented = True
            
        if not referral_documented:
            summary.append("No referral information documented during session")
            
        summary.append("")
        
        # Presenting Issues
        summary.append("## Presenting Issues:")
        presenting_issues = gpt_response.get("presenting_issues", [])
        if presenting_issues:
            for issue in presenting_issues:
                summary.append(f"- {issue}")
        else:
            summary.append("No presenting issues documented during session")
            
        summary.append("")
        
        # Diagnosis
        summary.append("## Diagnosis:")
        diagnosis = gpt_response.get("diagnosis", [])
        if diagnosis:
            for item in diagnosis:
                summary.append(f"- {item}")
        else:
            summary.append("No diagnosis documented during session")
            
        summary.append("")
        
        # Treatment Summary
        summary.append("## Treatment Summary:")
        treatment = gpt_response.get("treatment_summary", {})
        treatment_documented = False
        
        # Treatment duration
        if treatment.get("duration"):
            summary.append(f"- Duration of Therapy: {treatment['duration']}")
            treatment_documented = True
            
        # Number of sessions
        if treatment.get("sessions"):
            summary.append(f"- Number of Sessions: {treatment['sessions']}")
            treatment_documented = True
            
        # Type of therapy
        if treatment.get("therapy_type"):
            summary.append(f"- Type of Therapy: {treatment['therapy_type']}")
            treatment_documented = True
            
        # Therapeutic goals
        if treatment.get("goals") and len(treatment["goals"]) > 0:
            summary.append("- Therapeutic Goals:")
            for goal in treatment["goals"]:
                summary.append(f"  - {goal}")
            treatment_documented = True
            
        # Treatment description
        if treatment.get("description"):
            summary.append(f"- {treatment['description']}")
            treatment_documented = True
            
        # Medications
        if treatment.get("medications"):
            summary.append(f"- {treatment['medications']}")
            treatment_documented = True
            
        if not treatment_documented:
            summary.append("No treatment details documented during session")
            
        summary.append("")
        
        # Progress and Response to Treatment
        summary.append("## Progress and Response to Treatment:")
        progress = gpt_response.get("progress", {})
        progress_documented = False
        
        if progress.get("overall"):
            summary.append(f"- {progress['overall']}")
            progress_documented = True
            
        if progress.get("goal_progress") and len(progress["goal_progress"]) > 0:
            summary.append("- Progress Toward Goals:")
            for i, goal_progress in enumerate(progress["goal_progress"], 1):
                summary.append(f"  - Goal {i}: {goal_progress}")
            progress_documented = True
            
        if not progress_documented:
            summary.append("No treatment progress documented during session")
            
        summary.append("")
        
        # Clinical Observations
        summary.append("## Clinical Observations")
        observations = gpt_response.get("clinical_observations", {})
        observations_documented = False
        
        if observations.get("engagement"):
            summary.append(f"- Client's Engagement: {observations['engagement']}")
            observations_documented = True
            
        # Client's Strengths
        if observations.get("strengths") and len(observations["strengths"]) > 0:
            summary.append("- Client's Strengths:")
            for strength in observations["strengths"]:
                summary.append(f"  - {strength}")
            observations_documented = True
            
        # Client's Challenges
        if observations.get("challenges") and len(observations["challenges"]) > 0:
            summary.append("- Client's Challenges:")
            for challenge in observations["challenges"]:
                summary.append(f"  - {challenge}")
            observations_documented = True
            
        if not observations_documented:
            summary.append("No clinical observations documented during session")
            
        summary.append("")
        
        # Risk Assessment
        summary.append("## Risk Assessment:")
        if gpt_response.get("risk_assessment"):
            summary.append(f"- {gpt_response['risk_assessment']}")
        else:
            summary.append("No risk assessment documented during session")
            
        summary.append("")
        
        # Outcome of Therapy
        summary.append("## Outcome of Therapy")
        outcome = gpt_response.get("outcome", {})
        outcome_documented = False
        
        if outcome.get("current_status"):
            summary.append(f"- Current Status: {outcome['current_status']}")
            outcome_documented = True
            
        if outcome.get("remaining_issues"):
            summary.append(f"- Remaining Issues: {outcome['remaining_issues']}")
            outcome_documented = True
            
        if outcome.get("client_perspective"):
            summary.append(f"- Client's Perspective: {outcome['client_perspective']}")
            outcome_documented = True
            
        if outcome.get("therapist_assessment"):
            summary.append(f"- Therapist's Assessment: {outcome['therapist_assessment']}")
            outcome_documented = True
            
        if not outcome_documented:
            summary.append("No therapy outcome details documented during session")
            
        summary.append("")
        
        # Reason for Discharge
        summary.append("## Reason for Discharge")
        discharge_reason = gpt_response.get("discharge_reason", {})
        reason_documented = False
        
        if discharge_reason.get("reason"):
            summary.append(f"- Discharge Reason: {discharge_reason['reason']}")
            reason_documented = True
            
        if discharge_reason.get("client_understanding"):
            summary.append(f"- Client's Understanding and Agreement: {discharge_reason['client_understanding']}")
            reason_documented = True
            
        if not reason_documented:
            summary.append("No discharge reason documented during session")
            
        summary.append("")
        
        # Discharge Plan
        summary.append("## Discharge Plan:")
        if gpt_response.get("discharge_plan"):
            summary.append(f"- {gpt_response['discharge_plan']}")
        else:
            summary.append("No discharge plan documented during session")
            
        summary.append("")
        
        # Recommendations
        summary.append("## Recommendations:")
        recommendations = gpt_response.get("recommendations", {})
        recommendations_documented = False
        
        if recommendations.get("overall"):
            summary.append(f"- {recommendations['overall']}")
            recommendations_documented = True
            
        # Follow-Up Care
        if recommendations.get("followup") and len(recommendations["followup"]) > 0:
            summary.append("- Follow-Up Care:")
            for followup in recommendations["followup"]:
                summary.append(f"  - {followup}")
            recommendations_documented = True
            
        # Self-Care Strategies
        if recommendations.get("self_care") and len(recommendations["self_care"]) > 0:
            summary.append("- Self-Care Strategies:")
            for strategy in recommendations["self_care"]:
                summary.append(f"  - {strategy}")
            recommendations_documented = True
            
        if recommendations.get("crisis_plan"):
            summary.append(f"- Crisis Plan: {recommendations['crisis_plan']}")
            recommendations_documented = True
            
        if recommendations.get("support_systems"):
            summary.append(f"- Support Systems: {recommendations['support_systems']}")
            recommendations_documented = True
            
        if not recommendations_documented:
            summary.append("No recommendations documented during session")
            
        summary.append("")
        
        # Additional Notes
        summary.append("## Additional Notes:")
        if gpt_response.get("additional_notes"):
            summary.append(f"- {gpt_response['additional_notes']}")
        else:
            summary.append("No additional notes documented during session")
            
        summary.append("")
        
        # Final Note
        summary.append("## Final Note")
        if gpt_response.get("final_note"):
            summary.append(f"- Therapist's Closing Remarks: {gpt_response['final_note']}")
        else:
            summary.append("No final remarks documented during session")
            
        summary.append("")
        
        # Clinician Information
        clinician = gpt_response.get("clinician", {})
        if clinician.get("name"):
            summary.append(f"Clinician's Name: {clinician['name']}")
        else:
            summary.append("Clinician's Name: [clinician name]")
            
        summary.append("Clinician's Signature: [signature]")
        
        if clinician.get("date"):
            summary.append(f"Date: {clinician['date']}")
        else:
            # Use current date
            current_date = datetime.now().strftime("%B %d, %Y")
            summary.append(f"Date: {current_date}")
            
        summary.append("")
        
        # Attachments
        summary.append("## Attachments (if any)")
        attachments = gpt_response.get("attachments", [])
        if attachments:
            for attachment in attachments:
                summary.append(f"- {attachment}")
        else:
            summary.append("[None]")
        
        return "\n".join(summary)
    except Exception as e:
        error_logger.error(f"Error formatting discharge summary: {str(e)}", exc_info=True)
        return f"Error formatting discharge summary: {str(e)}"
    
async def format_pathology(gpt_response):
    """
    Format a pathology note from GPT structured response based on PATHOLOGY_NOTE_SCHEMA.
    
    Args:
        gpt_response: Dictionary containing the structured pathology note data
        
    Returns:
        Formatted string containing the human-readable pathology note
    """
    try:
        note = []
        
        # Add heading
        note.append("# PATHOLOGY NOTE\n")
        
        # Therapy session attended to section
        note.append("## Therapy session attended to")
        therapy_attendance = gpt_response.get("therapy_attendance", {})
        attendance_documented = False
        
        attendance_fields = [
            ("current_issues", "- "),
            ("past_medical_history", "- "),
            ("medications", "- "),
            ("social_history", "- "),
            ("allergies", "- ")
        ]
        
        for field, prefix in attendance_fields:
            if therapy_attendance.get(field) and therapy_attendance[field] != "Not documented":
                note.append(f"{prefix}{therapy_attendance[field]}")
                attendance_documented = True
                
        if not attendance_documented:
            note.append("No attendance details documented during this session.")
            
        note.append("")
        
        # Objective section
        note.append("## Objective:")
        objective = gpt_response.get("objective", {})
        objective_documented = False
        
        objective_fields = [
            ("examination_findings", "- "),
            ("diagnostic_tests", "- ")
        ]
        
        for field, prefix in objective_fields:
            if objective.get(field) and objective[field] != "Not documented":
                note.append(f"{prefix}{objective[field]}")
                objective_documented = True
                
        if not objective_documented:
            note.append("No objective findings documented during this session.")
            
        note.append("")
        
        # Reports section
        note.append("## Reports:")
        if gpt_response.get("reports") and gpt_response["reports"] != "Not documented":
            note.append(f"- {gpt_response['reports']}")
        else:
            note.append("No reports documented during this session.")
            
        note.append("")
        
        # Therapy section
        note.append("## Therapy:")
        therapy = gpt_response.get("therapy", {})
        therapy_documented = False
        
        therapy_fields = [
            ("current_therapy", "- "),
            ("therapy_changes", "- ")
        ]
        
        for field, prefix in therapy_fields:
            if therapy.get(field) and therapy[field] != "Not documented":
                note.append(f"{prefix}{therapy[field]}")
                therapy_documented = True
                
        if not therapy_documented:
            note.append("No therapy details documented during this session.")
            
        note.append("")
        
        # Outcome section
        note.append("## Outcome:")
        if gpt_response.get("outcome") and gpt_response["outcome"] != "Not documented":
            note.append(f"- {gpt_response['outcome']}")
        else:
            note.append("No outcomes documented during this session.")
            
        note.append("")
        
        # Plan section
        note.append("## Plan:")
        plan = gpt_response.get("plan", {})
        plan_documented = False
        
        plan_fields = [
            ("future_plan", "- "),
            ("followup", "- ")
        ]
        
        for field, prefix in plan_fields:
            if plan.get(field) and plan[field] != "Not documented":
                note.append(f"{prefix}{plan[field]}")
                plan_documented = True
                
        if not plan_documented:
            note.append("No plan documented during this session.")
        
        return "\n".join(note)
    except Exception as e:
        error_logger.error(f"Error formatting pathology note: {str(e)}", exc_info=True)
        return f"Error formatting pathology note: {str(e)}"

@app.post("/generate-custom-report")
@log_execution_time
async def generate_custom_report(
    transcript_id: str = Form(...),
    custom_template: str = Form(...),
    template_type: str = Form(...)  # <-- Add this field for template name/type
):
    """
    Generate a report using a custom template provided by the user.

    Args:
        transcript_id: ID of the transcript to use for report generation.
        custom_template: JSON string representing the custom template schema.
        template_type: Name/type of the custom template (provided by user).
    Returns:
        JSONResponse containing the generated report or an error message.
    """
    try:
        # Log incoming request details
        main_logger.info(f"Received custom report request - Transcript ID: {transcript_id}, Template Type: {template_type}")

        # Parse and validate custom template
        try:
            template_schema = json.loads(custom_template)
            main_logger.info("Custom template parsed successfully")
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format for custom template: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse({"error": error_msg}, status_code=400)

        # Retrieve transcript data
        table = dynamodb.Table('transcripts')
        response = table.get_item(Key={"id": transcript_id})

        if 'Item' not in response:
            return JSONResponse(
                {"error": f"Transcript ID {transcript_id} not found"},
                status_code=404
            )

        transcript_item = response['Item']

        # Decrypt and parse transcript data
        try:
            decrypted_transcript = decrypt_data(transcript_item.get('transcript', '{}'))
            transcription = json.loads(decrypted_transcript)
        except Exception as e:
            error_msg = f"Invalid transcript data format: {str(e)}"
            error_logger.error(error_msg)
            return JSONResponse(
                {"error": error_msg},
                status_code=400
            )

        # Generate report using the custom template
        formatted_report = await generate_report_from_transcription(transcription, template_schema)
        if isinstance(formatted_report, str) and formatted_report.startswith("Error"):
            return JSONResponse({"error": formatted_report}, status_code=400)

        # Add template_type as the main heading
        formatted_report = f"# {template_type}\n\n{formatted_report}"

        # Generate a unique report_id
        report_id = str(uuid.uuid4())

        # Prepare the report item for DynamoDB
        report_item = {
            "id": report_id,  # <-- This must match your DynamoDB table's primary key
            "template_type": template_type,
            "transcript_id": transcript_id,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "status": "completed",
            "custom_template": custom_template,
            "gpt_response": encrypt_data(json.dumps(template_schema).encode("utf-8")).decode("utf-8"),
            "formatted_report": formatted_report  # <-- Top-level field, not nested
        }

        # Store the report in DynamoDB (table: 'reports')
        report_table = dynamodb.Table('reports')
        report_table.put_item(Item=report_item)

        # Return the response in the required format
        return JSONResponse({
            "report_id": report_id,
            "template_type": template_type,
            "formatted_report": formatted_report
        })

    except Exception as e:
        error_msg = f"Unexpected error in generate_custom_report: {str(e)}"
        error_logger.exception(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

async def generate_report_from_transcription(transcription, template_schema):
    """
    Generate a report from transcription data using a custom template schema.
    
    Args:
        transcription: Dictionary containing the transcription data.
        template_schema: JSON schema provided by the user.
        
    Returns:
        Formatted report as a string or an error message.
    """
    try:
        # Prepare the system prompt
        system_prompt = """
        You are a medical documentation expert tasked with generating a comprehensive report from transcription data. 
        Follow the custom template provided meticulously, ensuring that each section is addressed with precision and clarity.

        Instructions:
        1. **Accuracy and Completeness**: Ensure all relevant information from the transcription is included. Cross-reference with the template to avoid missing any sections.
        2. **Medical Terminology**: Use standardized medical terminology and professional language consistent with healthcare practice in the USA, UK, Australia, and Canada. Employ appropriate clinical abbreviations where standard (e.g., PMH for past medical history, Hx for history).
        3. **Structure**: Format the report with clear headings. Use "# " for main headings and "## " for subheadings.
        4. **Content Style**: Present information in concise bullet points whenever possible. If data for a section is not available, write "Not documented during session" for that section.
        5. **Formatting**: Start directly with the report title/heading, without any introductory text like "Based on the transcript" or similar prefixes.
        6. **Clinical Precision**: Use precise clinical descriptions and avoid colloquial language. Maintain objectivity and clinical assessment language throughout.
        7. **Confidentiality**: Ensure that all patient information is handled with the utmost confidentiality and privacy.

        Your goal is to produce a clean, well-structured medical report using professional clinical language that healthcare professionals can quickly read and understand.
        """

        # Extract conversation text
        conversation_text = ""
        if "conversation" in transcription:
            for entry in transcription["conversation"]:
                speaker = entry.get("speaker", "Unknown")
                text = entry.get("text", "")
                conversation_text += f"{speaker}: {text}\n\n"

        # Generate the report using a language model API
        report = await generate_report_with_language_model(conversation_text, template_schema, system_prompt)

        return report

    except Exception as e:
        error_msg = f"Error generating report: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return f"Error: {error_msg}"

async def generate_report_with_language_model(conversation_text, template_schema, system_prompt):
    """
    Generate a report using a language model API.
    
    Args:
        conversation_text: The text of the conversation to be used in the report.
        template_schema: The custom template schema to follow.
        system_prompt: The system prompt guiding the report generation.
        
    Returns:
        A generated report as a string.
    """
    try:
        # Format the template schema as a string if it's a dictionary
        if isinstance(template_schema, dict):
            template_schema_str = json.dumps(template_schema, indent=2)
        else:
            template_schema_str = str(template_schema)
            
        # Use the global client with API key
        global client
        
        response = client.chat.completions.create(
            model="gpt-4", # or another appropriate model like "gpt-3.5-turbo"
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the conversation transcript:\n\n{conversation_text}\n\nTemplate Schema:\n{template_schema_str}\n\nPlease generate a report based on this template."}
            ],
            temperature=0.7,
            max_tokens=3500
        )
        
        # Extract the generated report from the response
        report = response.choices[0].message.content.strip()
        return report

    except Exception as e:
        error_msg = f"Error calling language model API: {str(e)}"
        error_logger.error(error_msg, exc_info=True)
        return f"Error: {error_msg}"

# Add this helper function near the top of your file (e.g., after imports or utility functions)
async def dynamodb_scan_all(table, **kwargs):
    """
    Scan a DynamoDB table and return all items, paginating as needed.
    """
    items = []
    last_evaluated_key = None

    while True:
        if last_evaluated_key:
            kwargs['ExclusiveStartKey'] = last_evaluated_key
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        last_evaluated_key = response.get('LastEvaluatedKey')
        if not last_evaluated_key:
            break
    return items




