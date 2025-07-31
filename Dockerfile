
# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed dependencies specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Run the uvicorn server when the container launches
# Assurez-vous que dropia_api.py est le nom de votre fichier principal
# et que 'app' est l'instance de votre application FastAPI
CMD ["uvicorn", "dropia_api:app", "--host", "0.0.0.0", "--port", "8000"]
