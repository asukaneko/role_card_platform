FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory
RUN mkdir -p data uploads

# Expose port
EXPOSE 7861

# Run with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:7861", "--workers", "4", "--timeout", "120", "server:app"]
