# ece-30861
ECE 30861 class project

## We switched from AWS Amplify to AWS Elastic Beanstalk
This was done so we can use Python to implement our backend.
Requirements.txt contains only Flask as of now. It will list any third-party Python packages we need to use.
Application.py exists solely to wrap all app objects as application objects. Elastic Beanstalk expects application objects,
not app objects. 

## For future updates
Enter the current version folder: Version_X
Select all files there
Compress to zip file
Upload that zip file into Elastic Beanstalk.
Zip file should contain:
Folder for HTML
Python files
Requirements.txt

Currently, requirements.zip contains the following:
    - templates folder
    - application.py
    - detection.py
    - main.py
    - requiresment.txt
    - watermark.py

This was uploaded to AWS Elastic Beanstalk. 
Current URL: http://aimark-env.eba-vhpawqa3.us-east-2.elasticbeanstalk.com/