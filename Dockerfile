# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
# Copy src directory
COPY src/ ./src/
# Copy prompts directory
COPY prompts/ ./prompts/
# Copy the entrypoint script (assuming it will be src/main.py)
COPY src/main.py .

# Define environment variables (can be overridden)
# Add /app/src to the PYTHONPATH so main.py can find modules in src/
ENV PYTHONPATH "${PYTHONPATH}:/app/src"

# Run main.py using its absolute path within the container when the container launches
ENTRYPOINT ["python", "/app/main.py"]
