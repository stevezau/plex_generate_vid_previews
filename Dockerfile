FROM linuxserver/ffmpeg:7.0.1

# Install Python and pip
RUN apt-get update && apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file to the working directory
COPY requirements.txt .

# Install the Python dependencies
ENV PIP_BREAK_SYSTEM_PACKAGES 1
RUN pip3 install -r requirements.txt

# Copy the Python script and .env file to the working directory
COPY plex_generate_previews.py .

# Run the Python script when the container starts
ENTRYPOINT ["/bin/bash", "-c", "/usr/bin/python3 /app/plex_generate_previews.py"]
