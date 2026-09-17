targetScope = 'resourceGroup'

param location string = 'westus2'
param repository string
@description('Email is metered. False disables email settings but does not delete existing email resources.')
param enableEmail bool = true

@secure()
@minLength(1)
param hereMapsApiKey string
@secure()
@minLength(32)
param appSecret string

var suffix = uniqueString(resourceGroup().id)
var resourceTags = {
  project: 'rolloff-ready'
  managedBy: 'rolloff-ready-bicep'
  repository: repository
}

resource site 'Microsoft.Web/staticSites@2024-04-01' = {
  name: 'rolloff-ready-${suffix}'
  location: location
  tags: resourceTags
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Disabled'
  }
}

resource email 'Microsoft.Communication/emailServices@2023-03-31' = if (enableEmail) {
  name: 'rolloff-email-${suffix}'
  location: 'global'
  tags: resourceTags
  properties: {
    dataLocation: 'United States'
  }
}

resource domain 'Microsoft.Communication/emailServices/domains@2023-03-31' = if (enableEmail) {
  parent: email
  name: 'AzureManagedDomain'
  location: 'global'
  tags: resourceTags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2023-03-31' = if (enableEmail) {
  name: 'rolloff-acs-${suffix}'
  location: 'global'
  tags: resourceTags
  properties: {
    dataLocation: 'United States'
    linkedDomains: [
      domain!.id
    ]
  }
}

var sender = enableEmail ? 'DoNotReply@${domain!.properties.mailFromSenderDomain}' : ''

resource appSettings 'Microsoft.Web/staticSites/config@2024-04-01' = {
  parent: site
  name: 'appsettings'
  properties: {
    HERE_MAPS_API_KEY: hereMapsApiKey
    APP_SECRET: appSecret
    ACS_CONNECTION_STRING: enableEmail ? communication!.listKeys().primaryConnectionString! : ''
    EMAIL_SENDER: sender
    CONTACT_RECIPIENT: 'jjfeore@gmail.com'
    ALLOWED_ORIGINS: 'https://${site.properties.defaultHostname}'
    APP_ENV: 'production'
  }
}

// Never output keys, connection strings, or the application secret.
output staticWebAppName string = site.name
output hostname string = site.properties.defaultHostname
output emailSender string = sender
