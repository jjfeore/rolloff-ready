targetScope = 'subscription'

@description('Dedicated resource group. Never point this project at a shared group.')
param resourceGroupName string = 'rg-rolloff-ready'
param location string = 'westus2'
@description('GitHub owner/repository used to verify ownership before updates and teardown.')
param repository string

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    project: 'rolloff-ready'
    managedBy: 'rolloff-ready-bicep'
    repository: repository
  }
}

output resourceGroupId string = group.id
