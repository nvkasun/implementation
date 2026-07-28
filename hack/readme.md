Read-only pre-apply check
aws dynamodb get-item \
  --table-name gg-eks-pipeline \
  --key '{
    "pipeline": {"S": "gg-oracle-payments-01"},
    "recordType": {"S": "CONFIG"}
  }' \
  --consistent-read \
  --region eu-west-1
aws dynamodb get-item \
  --table-name gg-eks-pipeline \
  --key '{
    "pipeline": {"S": "gg-postgresql-payments-01"},
    "recordType": {"S": "CONFIG"}
  }' \
  --consistent-read \
  --region eu-west-1

  aws dynamodb query \
  --table-name gg-eks-pipeline \
  --key-condition-expression "pipeline = :p" \
  --expression-attribute-values '{
    ":p": {"S": "gg-oracle-payments-01"}
  }' \
  --region eu-west-1