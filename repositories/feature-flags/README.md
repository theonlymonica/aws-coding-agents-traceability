# Feature flags

The feature flags of the shop are one SSM parameter with a JSON value. `template.yaml` is the
CloudFormation template that creates the parameter. The deployment broker deploys `template.yaml`
from the branch of the session.
