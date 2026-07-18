# CampusPath Resume PDF Processor

AWS SAM project that deploys a Lambda function to extract selectable text from
resume PDFs stored temporarily in S3.

## What it does

1. FastAPI uploads a PDF to `sagemaker-tutorials-mlhub` under `resume-uploads/`.
2. FastAPI invokes this Lambda with `{ bucket, key, filename }`.
3. Lambda downloads the object, extracts text with `pypdf`, and returns JSON.
4. FastAPI deletes the temporary S3 object and returns the text to the UI.

Scanned/image-only PDFs are not supported (no OCR / Textract in this version).

## Prerequisites

- AWS CLI configured for account/region `ap-south-1`
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Access to the existing bucket `sagemaker-tutorials-mlhub`

## Deploy

```bash
cd resume-pdf-processor
sam build
sam deploy --guided \
  --stack-name campuspath-resume-pdf-processor \
  --capabilities CAPABILITY_IAM \
  --region ap-south-1 \
  --parameter-overrides \
    ResumeBucketName=sagemaker-tutorials-mlhub \
    ResumeKeyPrefix=resume-uploads/
```

Note the stack outputs:

- `ResumePdfExtractorFunctionName` → export as `RESUME_EXTRACTOR_FUNCTION_NAME`
- `ResumeBucketName` → export as `RESUME_BUCKET_NAME`

## Environment variables for FastAPI

```bash
export AWS_REGION=ap-south-1
export RESUME_BUCKET_NAME=sagemaker-tutorials-mlhub
export RESUME_EXTRACTOR_FUNCTION_NAME=campuspath-resume-pdf-extractor
```

## IAM for the FastAPI runtime principal

Grant the identity that runs FastAPI:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::sagemaker-tutorials-mlhub/resume-uploads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:ap-south-1:*:function:campuspath-resume-pdf-extractor"
    }
  ]
}
```

The Lambda itself only receives `s3:GetObject` on that prefix.

## Local tests

```bash
cd resume-pdf-processor
python -m pip install -r src/requirements.txt pytest
python -m pytest tests -q
sam validate --lint
```

## Invoke manually

```bash
aws lambda invoke \
  --function-name campuspath-resume-pdf-extractor \
  --payload '{"bucket":"sagemaker-tutorials-mlhub","key":"resume-uploads/sample.pdf","filename":"sample.pdf"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/extract-out.json
cat /tmp/extract-out.json
```

## Limits

| Limit | Default |
| --- | --- |
| Max upload size | 5 MB |
| Max pages | 10 |
| Max returned characters | 20,000 |
| Supported content | Selectable-text PDFs only |
