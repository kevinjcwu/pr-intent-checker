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

# Make port 80 available to the world outside this container (if needed, unlikely for this action)
# EXPOSE 80

# Define environment variables (can be overridden)
# ENV NAME World

# Run main.py when the container launches
ENTRYPOINT ["python", "main.py"]
