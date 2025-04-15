import ssl
import certifi
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID
from datetime import datetime
from typing import Tuple, Optional, Dict, Any

def create_ssl_context(verify_mode=ssl.CERT_REQUIRED) -> ssl.SSLContext:
    """
    Create a secure SSL context with proper certificate validation.
    
    Args:
        verify_mode: SSL verification mode
        
    Returns:
        Configured SSL context
    """
    context = ssl.create_default_context(cafile=certifi.where())
    context.verify_mode = verify_mode
    context.check_hostname = True
    
    # Set secure protocol options
    context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    
    return context

def validate_certificate(cert_data: bytes) -> Tuple[bool, Dict[str, Any]]:
    """
    Enhanced certificate validation with detailed checks.
    
    Args:
        cert_data: The certificate data in bytes
        
    Returns:
        Tuple of (is_valid, details)
    """
    try:
        cert = x509.load_der_x509_certificate(cert_data, default_backend())
        
        # Check expiration
        now = datetime.utcnow()
        is_expired = now > cert.not_valid_after
        is_not_yet_valid = now < cert.not_valid_before
        
        # Get certificate details
        try:
            common_name = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except IndexError:
            common_name = "Unknown"
        
        details = {
            "common_name": common_name,
            "issuer": cert.issuer.rfc4514_string(),
            "serial_number": cert.serial_number,
            "not_valid_before": cert.not_valid_before,
            "not_valid_after": cert.not_valid_after,
            "is_expired": is_expired,
            "is_not_yet_valid": is_not_yet_valid,
        }
        
        return not (is_expired or is_not_yet_valid), details
    
    except Exception as e:
        return False, {"error": str(e)}
