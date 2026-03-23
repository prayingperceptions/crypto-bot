import logging
import sys

def setup_logger(name: str = "crypto_bot") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Stream Handler
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        stdout_handler.setFormatter(formatter)
        
        # File Handler
        file_handler = logging.FileHandler("bot.log")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        
        logger.addHandler(stdout_handler)
        logger.addHandler(file_handler)
        
    return logger
