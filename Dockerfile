FROM jrottenberg/ffmpeg:4.2-nvidia

# Install Python and pip
RUN apt-get update && apt-get install -y python3 python3-pip mediainfo

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file to the working directory
COPY requirements.txt .

# Install the Python dependencies
RUN pip3 install -r requirements.txt

# Copy the Python script and .env file to the working directory
COPY plex_generate_previews.py .

# Run the Python script when the container starts
ENTRYPOINT ["python3", "plex_generate_previews.py"]
