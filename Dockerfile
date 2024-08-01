FROM linuxserver/ffmpeg:7.0.1

# Install Python and pip
RUN apt-get update && apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip

# Install libs required
RUN apt-get install -y libdav1d-dev nvidia-driver-525 nvidia-cuda-toolkit build-essential yasm cmake libtool libc6 libc6-dev unzip wget libnuma1 libnuma-dev openssl libssl-dev libass-dev libfdk-aac-dev libmp3lame-dev libopus-dev libtheora-dev libvorbis-dev libvpx-dev libx264-dev libx265-dev

# Rebuild FFMPEG
RUN mkdir /ffmpeg; cd /ffmpeg
RUN git clone https://git.ffmpeg.org/ffmpeg.git /ffmpeg/ --depth=1
RUN ./configure --disable-debug --enable-cuda-nvcc --enable-libnpp --extra-cflags=-I/usr/local/cuda/include --extra-ldflags=-L/usr/local/cuda/lib64 --disable-doc --disable-ffplay --enable-alsa --enable-cuvid --enable-ffprobe --enable-gpl --enable-libaom --enable-libass --enable-libfdk_aac --enable-libfontconfig --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libkvazaar --enable-libmp3lame --enable-libopencore-amrnb --enable-libopencore-amrwb --enable-libopenjpeg --enable-libopus --enable-libplacebo --enable-librav1e --enable-libshaderc --enable-libsvtav1 --enable-libtheora --enable-libv4l2 --enable-libvidstab --enable-libvmaf --enable-libvorbis --enable-libvpl --enable-libvpx --enable-libwebp --enable-libx264 --enable-libx265 --enable-libxml2 --enable-libxvid --enable-libzimg --enable-nonfree --enable-nvdec --enable-nvenc --enable-cuda-llvm --enable-opencl --enable-openssl --enable-stripping --enable-vaapi --enable-vdpau --enable-version3 --enable-vulkan
RUN make && sudo make install

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
